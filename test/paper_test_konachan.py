import asyncio
import httpx
from loguru import logger
from bs4 import BeautifulSoup
from pathlib import Path
import random
import time

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    ),
    "Referer": "https://konachan.com/post/popular_by_month?month=9&year=2026",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-CN;q=0.7",
    "Cookie": "__utmz=235754395.1788867884.1.1.utmcsr=(direct)|utmccn=(direct)|utmcmd=(none); __utma=235754395.558794313.1788867884.1789123956.1789294142.3; __utmc=235754395; mode=view; vote=1; __utmt=1; cf_clearance=puyDB6bww8Vbb9.yHSqdmCC77Z5mjUW1hCJh6U8gAy0-1789298880-1.2.1.1-iIGVEa1g6sb4TdsBnwWD9gmwa8YU98jYhmZjFzCvq2m5UdpPVt_XCPHhsGKmTTTCFezOmaEtHmkfnWED95cngp0_hIjqqF3m9jXaKluonvhQOMM5W6SYk40eLRWnfcz_7qHbylb5G_zSEu9xHz_8udzDmK.Qn3YVsNxPtekEezVAmvwhjHaH30zIJplhNe9Mtj1xD1qtnKTL1WwZzu3Ktn1QFRfHYlOQ3DkeuVcu5.KcBtlOuVJJjK1WJOUGyOLSEs8gWvyDUARXsYoZGyJt_rG59xZwI3utjjF0r5Q66XurppfYTjZ.VGGjoEm_GW61IwGe1_It9VHfDziShD03QASVr3qzXwMByyZdbnMnyQBTooLQs3TE3aDCqVCjB7VI_2G5gc2aVnyA.ElzMwN_FCFGH49d_jVWb4iY2Mrx0frgXhdRVPZSJuGEMzFDrkTZHsQqdRAWb2YbO2Hx.MQeag; session_konachan-com=uZY9mpGi9dYfk7v572lElwaU4av6W6hkHbA1juOxbf3kwU50CmFaX2gmIrJ%2BBkiVFB5inHGtQwQVMfrsZtYs%2BHraq%2BL0UUPz4XepMCebsYXa90nQ8L%2F8jKie7AFPk5Tsp0f%2BaucRY4r2aiIRFMTbhF7NI37j0jjz77c%2BfxizjuEeu9VqbKs1P%2Fx0ox7t%2F%2BeD%2FaoF6Iy76shJ5ts9uNLgD%2B7MRS7voBKYL1hJ5haKbvWMiR7mouvYSy5CryAP9KBQwGRPWOJHznQsU6U2KRTcw2nXE6L4TNTod4OFF3KbYyBtbPqvgzBUqIwOKKNLJJw6gw0%3D--CnUnLCALKzVDD%2F2C--unOSrczJ4dCzNS%2BTRcz90Q%3D%3D; __utmb=235754395.39.10.1789294142",
}

class RateLimiter:
    def __init__(self, rate):
        self.interval = 1 / rate
        self.lock = asyncio.Lock()
        self.last_request = 0

    async def acquire(self):
        async with self.lock:
            wait = self.interval - (time.monotonic() - self.last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_request = time.monotonic()


async def request_with_retry(client, url, semaphore, limiter, params=None, max_retries=3, stream=False):
    for retry in range(max_retries):
        try:
            async with semaphore:
                await limiter.acquire()

                if stream:
                    request = client.build_request("GET", url, params=params)
                    response = await client.send(request, stream=True)
                else:
                    response = await client.get(url, params=params)

            if response.status_code == 429:
                delay = 15 * 2 ** retry
                logger.warning(f"429: {url}, retry in {delay}s")
                if stream:
                    await response.aclose()
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 500:
                delay = 2 ** retry + random.uniform(0, 1)
                logger.warning(f"{response.status_code}: {url}, retry in {delay:.1f}s")
                if stream:
                    await response.aclose()
                await asyncio.sleep(delay)
                continue

            response.raise_for_status()
            return response

        except httpx.TimeoutException as e:
            logger.warning(f"Timeout: {url}: {e}")

        except httpx.RequestError as e:
            logger.warning(f"Network error: {url}: {e}")

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error: {url}: {e.response.status_code}")
            return None

        if retry < max_retries - 1:
            await asyncio.sleep(2 ** retry + random.uniform(0, 1))

    logger.error(f"Failed after {max_retries} attempts: {url}")
    return None


def process_html(html_content):
    results = []
    soup = BeautifulSoup(html_content, "html.parser")

    for li in soup.select("li[id^='p']"):
        post_id = li.get("id")
        link = li.select_one("a.directlink.largeimg")

        if not post_id or link is None:
            continue

        results.append({
            "id": post_id.removeprefix("p"),
            "id_url": f"https://konachan.com/post/show/{post_id.removeprefix('p')}",
            "url": link["href"],
        })

    return results


async def get_info(page, client, semaphore, limiter):
    response = await request_with_retry(
        client,
        "https://konachan.com/post",
        semaphore,
        limiter,
        params={"page": page},
    )

    if response is None:
        return []

    results = process_html(response.content)
    logger.info(f"Page {page}: found {len(results)} items")
    return results


async def get_hot_pic_info(client, length, semaphore, limiter, year=1, month=1, day=1):
    if length == "month":
        url = "https://konachan.com/post/popular_by_month"
        params = {"year": year, "month": month}

    elif length == "week":
        url = "https://konachan.com/post/popular_by_week"
        params = {"year": year, "month": month, "day": day}

    else:
        logger.error("length must be 'month' or 'week'")
        return []

    response = await request_with_retry(client, url, semaphore, limiter, params=params)

    if response is None:
        return []

    results = process_html(response.content)
    logger.info(f"Popular {length}: found {len(results)} items")
    return results


async def load_pic(pic_info, pic_dir, client, semaphore, limiter):
    pic_url = pic_info["url"]
    pic_id = pic_info["id"]
    suffix = Path(pic_url.split("?")[0]).suffix or ".jpg"
    save_path = Path(pic_dir) / f"{pic_id}{suffix}"

    if save_path.exists() and save_path.stat().st_size > 0:
        return {
            "id": pic_id,
            "status": "skipped",
            "path": str(save_path),
            "error": None,
        }

    save_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = save_path.with_suffix(save_path.suffix + ".part")

    response = await request_with_retry(
        client,
        pic_url,
        semaphore,
        limiter,
        stream=True,
    )

    if response is None:
        return {
            "id": pic_id,
            "status": "failed",
            "path": None,
            "error": "request failed",
        }

    try:
        with open(temp_path, "wb") as f:
            async for chunk in response.aiter_bytes(256 * 1024):
                f.write(chunk)

        temp_path.replace(save_path)
        logger.info(f"Saved: {save_path}")

        return {
            "id": pic_id,
            "status": "success",
            "path": str(save_path),
            "error": None,
        }

    except OSError as e:
        logger.error(f"File error: {save_path}: {e}")

        if temp_path.exists():
            temp_path.unlink()

        return {
            "id": pic_id,
            "status": "failed",
            "path": None,
            "error": str(e),
        }

    finally:
        await response.aclose()


async def get_hot_pic(config, pic_dir):
    page_semaphore = asyncio.Semaphore(2)
    download_semaphore = asyncio.Semaphore(5)

    page_limiter = RateLimiter(1)
    download_limiter = RateLimiter(2)

    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=20,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
    ) as client:

        hot_pics = await get_hot_pic_info(
            client,
            config.get("length", "month"),
            page_semaphore,
            page_limiter,
            year=config.get("year", 1),
            month=config.get("month", 1),
            day=config.get("day", 1),
        )

        tasks = [
            load_pic(pic, pic_dir, client, download_semaphore, download_limiter)
            for pic in hot_pics
        ]

        results = await asyncio.gather(*tasks)

    success = sum(x["status"] == "success" for x in results)
    skipped = sum(x["status"] == "skipped" for x in results)
    failed = sum(x["status"] == "failed" for x in results)

    logger.info(
        f"Finished: total={len(results)}, "
        f"success={success}, skipped={skipped}, failed={failed}"
    )

    return results


async def main():
    config = {
        "length": "month",  # Options: 'month', 'week'
        "year": 2026,
        "month": 9,
        "day": 6,
    }
    pic_dir = "../pics/konachan/month"
    for i in range(5, 8):
        config["month"] = i
        await get_hot_pic(config, pic_dir)
    # await get_hot_pic(config, pic_dir)

asyncio.run(main())