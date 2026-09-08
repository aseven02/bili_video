import httpx
import requests
import json
from tqdm import tqdm
from bs4 import BeautifulSoup
import os
from concurrent.futures import ThreadPoolExecutor
from loguru import logger

def judge_valid_url(url):
    try:
        response = httpx.head(
            url,
            follow_redirects=True,
            timeout=10
        )
        return response.status_code == 200
    except httpx.RequestError:
        return False

def trans_to_full_url(pic_small_url):
    pic_id = pic_small_url.split('/')[-1].split('.')[0]
    url_without_extension = f"https://w.wallhaven.cc/full/{pic_id[0:2]}/wallhaven-{pic_id}"
    if judge_valid_url(url_without_extension + ".jpg"):
        return url_without_extension + ".jpg"
    elif judge_valid_url(url_without_extension + ".png"):
        return url_without_extension + ".png"
    else:
        logger.error(f"Failed to get full URL for {pic_small_url}")
        return None

def get_pic_small_url_result(params):

    base_url = "https://wallhaven.cc/search"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3',
        'referrer': 'https://wallhaven.cc/',
        'cookie': '_pk_id.1.01b8=81b0abaa1121b7f7.1788856558.; remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d=eyJpdiI6ImNzcTdhMzNESWc3dkp1emZVXC90dXV3PT0iLCJ2YWx1ZSI6IkYwYkZYQTFGbUtXNkRPbEtmUlhpalZ2QkxrS0c4dklaT1N1QzBrQU5mendkYkMxNjhDQXB3YUROZFwvbFA4blk3QlEzVWljRWQ1SCtOWUFuVnF6cnB4XC90YzEyZjZPSlRSZTRaTEl0MGtWekljS1BvdDYzbWRxeCtQeEVybFgzXC9PbThGZlpMYk5EeVdmVzhxVlZVYUtyKzBRbnJRNlwvaG1vTGl5aUdNT1VYRlh2QjFpWUVCcWpHbmIyTXdFaWFIR3kiLCJtYWMiOiIzNGJjNjk3MjE1Yjg0Mjc5OTU0ZTQyODZmNzM0ZjZmMTQxNTMwNDgwNzE2N2NmNGIxYjUxNTMwZTA5N2M1YmNlIn0%3D; _pk_ref.1.01b8=%5B%22%22%2C%22%22%2C1788859943%2C%22https%3A%2F%2Fwww.google.com%2F%22%5D; _pk_ses.1.01b8=1; cf_clearance=uCLCq_sIIE7bH4tJMzcQhRFArCwzpWVCHhFCAEPaFWU-1788860667-1.2.1.1-aPxKFnRzpwnz79sh3dUYOXtugoNN775FFHYMZJiNC7j7J1XidEt20B7agTqlc0xU7Iq1_11tkwLpt.R1zh5uID1mpXZWl47.7cmE5Nw8ZOmFpNp2Ugbw3PxxSJL_tcmN.VmjY7ELpj7XhP8Z8FrVor5Gb8BYW0BH1zYgERy16RSL_fhqUS8xpm16bQsJf6Kdvgad6srjDx.qH3XBubkKjgmh0nP30Bvn1aw.BckT4B4ln1ESNo3JN5.pXE87WWZsSrpGvauAk9gqYfFvBN.dURWzTPgUYo0y81Y7ujIQ3w7Tip17uoJJgte.V2GEv_LHSLsuSusBXGo7qf3wnPjdU8RuPUXitRHxF308.yZD5I2FbjvwN6oMD3HzUFhwmyxCzOgHejynL7c1wER9165XVt5yID5uhuyQkYJh4Nw1bDhth3dcityPRFi9Qlg0KNWaladJODpiyslEP7_K.kH2gA; XSRF-TOKEN=eyJpdiI6Imh3K2JZQ0IrdXl0RnFmQVZTc01lQXc9PSIsInZhbHVlIjoiR2FFZ2FXQnFnaFpaNk83bDhDS2xcL2d4Z0tuTEg3b0dZQm80cXlUXC9reU5mek5WZzVQQkMrVUxFSmlEWndHWGE2IiwibWFjIjoiMWIwOTY4NTAxYzJlMmRkM2U5Y2Y4MGNhNTMwZGI0NWE4NTFjMzVlYmZjYTRhY2Y4MzFlOTE5MTE4MTBlYWQ0OCJ9; wallhaven_session=eyJpdiI6IlwvSm02UlwvSEQwbkRMY0J2WjY0RzhFUT09IiwidmFsdWUiOiJMVngrcHNXNHVcL2l6QUFYaGJnVG1UdWV0ZTViVzArQVZ1elhMa0l1andGUkJEcUhUS1ZDSFFqK3lyREJNcTNQVyIsIm1hYyI6ImYyMTY5ZmFmNTE3ZjczN2QzYTBhNmMwZDFkN2EyYTVlMDlmMjYxYmM3ZTM0NWUyMjA1MDM4NTM3NTMwYWFlZmEifQ%3D%3D'
    }

    response = httpx.get(base_url, params=params, headers=headers)
    soup = BeautifulSoup(response.text, 'html.parser')
    all_pic = soup.find_all('figure')
    if all_pic:
        pic_small = []
        for pic in all_pic:
            pic_small_url = pic.find('img')['data-src']
            if pic_small_url:
                pic_small.append(pic_small_url)
            else:
                logger.error("No small picture URL found for a picture.")
                return []
        return pic_small
    else:
        logger.error("No pictures found on the page.")
        return []

def save_pic(pic_url, pic_dir):
    try:
        response = httpx.get(pic_url, timeout=10)
        response.raise_for_status()
        save_path = f"{pic_dir}/{pic_url.split('/')[-1]}"
        os.makedirs(pic_dir, exist_ok=True)
        image_data = response.content
        if image_data:
            with open(save_path, 'wb') as f:
                f.write(image_data)
            logger.info(f"Saved image to {save_path}")
        else:
            logger.error(f"Failed to save image from {pic_url}")
    except Exception as e:
        logger.error(f"Request error while downloading {pic_url}: {e}")
        return None
    

def process_pic_small_url(pic_small_url, pic_dir, save_choiece='both'):
    full_url = trans_to_full_url(pic_small_url)
    if save_choiece == 'both' or save_choiece == 'small':
        save_pic(pic_small_url, pic_dir)
    if save_choiece == 'both' or save_choiece == 'full':
        save_pic(full_url, f'{pic_dir}/full')
    return full_url

def get_result_list(params, pages, pic_dir='pics', save_choiece='both'):
    result_list = []
    for page in range(1, pages + 1):
        params['page'] = page
        pic_urls = get_pic_small_url_result(params)
        logger.info(f"Page {page}: Found {len(pic_urls)} pictures.")
        if pic_urls:
            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_url = {executor.submit(process_pic_small_url, pic_small, pic_dir, save_choiece): pic_small for pic_small in pic_urls}
                for future in future_to_url:
                    pic_small = future_to_url[future]
                    try:
                        full_url = future.result()
                        if full_url:
                            result_list.append((pic_small, full_url))
                    except Exception as e:
                        logger.error(f"Error processing {pic_small}: {e}")
        else:
            logger.error(f"No pictures found on page {page}.")
        with open(f'{pic_dir}/pic_url_test.json', 'w', encoding='utf-8') as f:
            json.dump(result_list, f, ensure_ascii=False, indent=4)
    return result_list


src="https://w.wallhaven.cc/full/k8/wallhaven-k8dggq.jpg" 

params = {
        'toprange': '1M',
        'categories': '010',
        'purity': '011',
        'sorting': 'toplist',
        'order': 'desc',
        'page': 1
}
get_result_list(params, pages=3, pic_dir='../pics/toplist', save_choiece='full')