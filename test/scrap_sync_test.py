import httpx
import asyncio
import json
from bs4 import BeautifulSoup
from loguru import logger
import random
import os

def process_page_html(html):
    """解析页面HTML,提取所有合同的信息(title + url)"""
    soup = BeautifulSoup(html, 'html.parser')

    results = [{
            "title": item.get("article-title"),
            "url": item.get("article-url"),
        } for item in soup.find_all("listing-titles-only")
    ]

    return results


def process_contact_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    
    body_div = soup.find('div', {'class': 'body'})
    results = []
    if not body_div:
        return results

    branch = None
    for p in body_div.find_all('p'):
        style = p.get('style', '')
        text = p.get_text(strip=True)
        if 'center' in str(style):
            # 提取当前主题
            '''未解决的小问题:
                1.19年8月30号左右和7月14号左右用的样式是 aligin: center ,该日期附近有几合同爬取结果为空
                2.19年4月1号左右用的标题和内容都是div标签,不是p标签,附近有几个合同爬取结果为空
                3.别的都是比较零散的极个别问题'''
            # 18年和19年有部分文章用与p样式一样的空值&nbsp更新title，阻断爬取,所以加一个判断,为空值时，保留上一个主题
            # 17年的文章，在开头有一个与p样式一样的 CONTRACTS 小标题，遇到直接跳过
            if text and text != 'CONTRACTS':
                branch = text
            continue

        # 提取内容到主题对应的列表
        content = p.get_text(strip=True)
        # 这里清除了p标签内的 *Small 等标识
        if content.startswith('*Small') or content.startswith('* Small'):
            break
        results.append(
            {
                "branch": branch,
                "content": content
            }
        )
    return results
    

class RateLimiter:
    """限速器, 用于控制请求频率"""
    def __init__(self, rate):
        self.interval = 1.0 / rate
        self.lock = asyncio.Lock()
        self.last_request_time = 0

    async def acquire(self):
        async with self.lock:
            now = asyncio.get_event_loop().time()
            wait = self.interval - (now - self.last_request_time)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_request_time = asyncio.get_event_loop().time()


class Scraper:
    """主爬虫"""
    def __init__(self, cookie, rate_limit=5, semaphore_limit=5,):
        
        self.semaphore = asyncio.Semaphore(semaphore_limit)  # 控制并发请求数
        self.limiter = RateLimiter(rate=rate_limit)  # 控制请求频率
        self.quene = asyncio.Queue(100)  # 限制队列长度为100，防止过多请求堆积
        self.result_quene = asyncio.Queue(100)  # 用于存储处理后的结果

        self.client = httpx.AsyncClient(
            timeout=20.0,
            headers={
                "User-Agent": 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36',
                "Cookie": cookie,
            }
        )

    async def request_with_retry(self, url, max_retries=3):
        """带重试的请求"""
        for retry in range(max_retries):
            try:
                await self.limiter.acquire()
                async with self.semaphore:
                    response = await self.client.get(url)

                if response.status_code == 429:
                    delay = 15 * 2 ** retry
                    logger.warning(f"429: {url}, retry in {delay}s")
                    await asyncio.sleep(delay)
                    continue

                if response.status_code >= 500:
                    delay = 2 ** retry + random.uniform(0, 1)
                    logger.warning(f"{response.status_code}: {url}, retry in {delay:.1f}s")
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
            except Exception as e:
                logger.error(f"Request unexpected error: {url}: {e}")
                return None
            
        logger.error(f"Failed after {max_retries} attempts: {url}")
        return None

    async def get_page_urls(self, page):
        """获取页面中所有url"""
        base_url = 'https://www.war.gov/News/Contracts/'
        params = {'page': page}
        response = await self.request_with_retry(f"{base_url}?page={page}")
        if response is None:
            logger.error(f"Failed to fetch page {page}")
            return []
        else:
            urls = process_page_html(response.text)
            logger.info(f"Page {page} urls num: {len(urls)}")
            return urls

    async def process_contact_url(self, url_info):
        """解析并处理具体合同的url"""
        contract_id = url_info['title']
        url = url_info['url']
        response = await self.request_with_retry(url)
        if response is None:
            logger.error(f"Failed to fetch contact url: {url}, contract_id: {contract_id}")
            return None
        else:
            contact_results = process_contact_html(response.text)
            if not contact_results:
                logger.warning(f"No contact results found for contract_id: {contract_id}, url: {url}")
                return []
            return [{
                        'contract_id': contract_id,
                        'url': url,
                        'branch': contact.get('branch', ''),
                        'content': contact.get('content', '')
                    } for contact in contact_results
                ]


    async def producer(self, start_page, end_page, output_file):
        """生产者: 获取页面中的所有合同url"""
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, 'r', encoding='utf-8') as f:
            existing_results = [json.loads(line) for line in f if line.strip()]
            existing_contract_ids = set(result['contract_id'] for result in existing_results)
            logger.info(f"Loaded {len(existing_contract_ids)} existing contract IDs from {output_file}")

        tasks = {
            asyncio.create_task(self.get_page_urls(page)): page 
            for page in range(start_page, end_page + 1)
        }

        # 哪一页先完成，就先处理哪一页
        async for task in asyncio.as_completed(tasks):
            page = tasks[task]
            urls = await task
            added = 0
            for url_info in urls:
                contract_id = url_info["title"]

                if contract_id in existing_contract_ids:
                    continue

                await self.quene.put(url_info)
                added += 1

            logger.info(
                f"Page {page} fetched {len(urls)} urls, added {added} urls to queue"
            )


    async def consumer(self):
        """消费者: 处理队列中的合同url"""
        while True:
            try:
                url_info = await self.quene.get()
                if url_info is None:
                    break
                contact_results = await self.process_contact_url(url_info)
                if contact_results:
                    for contact in contact_results:
                        await self.result_quene.put(contact)
                    logger.info(f"Processed contract: {url_info['title']}")
            finally:
                self.quene.task_done()

    async def write_results(self, filepath):
        """将结果写入文件"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        with open(filepath, 'a', encoding='utf-8') as f:
            while True:
                try:
                    result = await self.result_quene.get()
                    if result is None:
                        break
                    json.dump(result, f, ensure_ascii=False)
                    f.write('\n')
                except Exception as e:
                    logger.error(f"Error writing result: {e}")
                finally:
                    self.result_quene.task_done()

    async def run(self, start_page, end_page, output_file):
        """运行爬虫"""
        producer_task = asyncio.create_task(self.producer(start_page, end_page, output_file))
        consumer_tasks = [
            asyncio.create_task(self.consumer()) for _ in range(5)
        ]
        writer_task = asyncio.create_task(self.write_results(output_file))

        await producer_task
        await self.quene.join()  # 等待队列处理完毕
        for _ in range(5):
            await self.quene.put(None)  # 发送结束信号给消费者
        await asyncio.gather(*consumer_tasks)
        await self.result_quene.join()  # 等待结果队列处理完毕
        await self.result_quene.put(None)  # 发送结束信号给写入任务
        await writer_task
        await self.client.aclose()  # 关闭客户端
            

async def main():
    cookie = "_ga=GA1.1.784025586.1789196060; _ga_CSLL4ZEK4L=GS2.1.s1789562503$o5$g1$t1789562554$j9$l0$h0; _ga_SB6KFHKWNW=GS2.1.s1789562504$o5$g1$t1789562554$j10$l0$h1116168499"
    scraper = Scraper(cookie=cookie, rate_limit=4, semaphore_limit=5)
    await scraper.run(start_page=1, end_page=12, output_file='test/output/contracts.jsonl')

if __name__ == "__main__":
    asyncio.run(main())