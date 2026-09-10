import httpx
from concurrent.futures import ThreadPoolExecutor, as_completed
from loguru import logger
import json
import os
import time
import random
from datetime import datetime

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3',
    'referrer': 'https://anime-pictures.net/',
    'cookie': 'sitelang=zh-cn; anime_pictures_jwt=eyJ0eXAiOiJKV1QiLCJhbGciOiJFZERTQSJ9.eyJ1c2VyIjp7ImlkIjoyODU4NTAsImxvZ2luIjoiYXNldmVuIiwiZ3JvdXBzIjpbInVzZXIiXX0sImV4cCI6MTgxOTk3MTc4OCwiaWF0IjoxNzg4ODY3Nzg4fQ.CVjIkc7h0wOQC-AIhh0v-UGUMg4g3e3km6aJQptydOdTNqLY2apjRNLmCe6w4qFyXc5LL0aBOT5MkL592ylQAA; time_zone=Asia%2FShanghai; cf_clearance=7WimGaJGwJnHFyVtsGcqH.hAwHuJQ9ttZONDTKEx4zY-1788867818-1.2.1.1-DO1bt1NP7CWr85aNmRZBNShS69RlzIwLfmydSq3_6wdR54.Sx47on5NJ4c6WOcGI9x0Qyc_nFQqCFi9nniq51pkec5LPI69bz9jNtG3GoA76a4POtDzKRtxDY7Ia9XJ0Gx8J1EcPAIGbaofXQffBZDw87fuS2sjeg66jAfASVVggVC5YbOiaZ08odrtN7IE77r74JMeP66d71ltiXuZeuuWW8YHTUos7I3aWN5YcGH6FIvtw2i_ngRHeniRRpZuQc4IDSodC3vKliCZkIqR4t6QJCpOrHbxPqhEmIWud2bwPdk0aaug3zVe6CGROscM50geuu0qUEIqeYumpVLVYhcWH4XYynB6W5pY2jhVstGiy43f3jYfAWDTG9bVG0pc_rSpuJp2LaNzI1Fnb0lgh05WYJZTxKuGnzJ9tixSFfS0RXqfZtyRePjTb3LuAr_16735IXhLPSPJhzZU.T1tDm8JSZYbIUYbdQn1mifwi2AxLd8mb7QOvah3P7Rw4_jML2aJLFsfpMfxHz32Wpri.ow; kira=1'
}

def get_pic_info(response_data):
    base_url = 'https://oimages.anime-pictures.net'
    small_base_url = 'https://opreviews.anime-pictures.net'
    if isinstance(response_data, dict) and 'md5' in response_data:
        md5 = response_data['md5']
        ext = response_data.get('ext', '')
        url = f'{base_url}/{md5[:3]}/{md5}{ext}'
        small_url = f'{small_base_url}/{md5[:3]}/{md5}_lp.avif'
        return {
            'pic_id': response_data.get('id'),
            'url': url,
            'small_url': small_url,
            'score': response_data.get('score', 0),
            'score_count': response_data.get('score_count', 0),
            'download_count': response_data.get('download_count', 0),
            'erotics': response_data.get('erotics', 0),   
        }
    return None

def save_pic(pic_url, pic_dir, pic_id, max_retries=3, client=None):
    requester = client if client else httpx
    for retry in range(max_retries):
        try:
            response = requester.get(pic_url, headers=headers, timeout=10)
            if response.status_code == 429:
                logger.warning(f"Rate limited while downloading {pic_url}. Retrying after a delay.{retry + 1}/{max_retries}")
                if response.headers.get('Retry-After'):
                    logger.info(f"Retry-After header found: {response.headers['Retry-After']} seconds.")
                    retry_after = int(response.headers['Retry-After'])
                    time.sleep(retry_after)
                else:
                    time.sleep(30*(2**retry))
                continue

            response.raise_for_status()
            save_path = f"{pic_dir}/{pic_id}.{pic_url.split('.')[-1]}"
            os.makedirs(pic_dir, exist_ok=True)
            image_data = response.content
            if image_data:
                with open(save_path, 'wb') as f:
                    f.write(image_data)
                logger.info(f"Saved image to {save_path}")
                break
            else:
                logger.error(f"Failed to save image from {pic_url}")
            
        except Exception as e:
            logger.error(f"Request error while downloading {pic_url}: {e}")
            return None
    return None

def process_single_pic(pic_info, pic_dir, save_choice='both'):
    if not pic_info:
        logger.error("Invalid picture data.")
        return
    if save_choice == 'both' or save_choice == 'small':
        if os.path.exists(f'{pic_dir}/small/{pic_info["pic_id"]}.{pic_info["small_url"].split(".")[-1]}'):
            logger.info(f"Small image for pic_id {pic_info['pic_id']} already exists. Skipping download.")
            return
        save_pic(pic_info['small_url'], f'{pic_dir}/small', pic_info['pic_id'])
    if save_choice == 'both' or save_choice == 'full':
        if os.path.exists(f'{pic_dir}/full/{pic_info["pic_id"]}.{pic_info["url"].split(".")[-1]}'):
            logger.info(f"Full image for pic_id {pic_info['pic_id']} already exists. Skipping download.")
            return
        save_pic(pic_info['url'], f'{pic_dir}/full', pic_info['pic_id'])
        time.sleep(random.uniform(1, 2))  # To avoid overwhelming the server
    

def get_info_from_posts(page):
    base_url = 'https://api.anime-pictures.net/api/v3/posts'
    try:
        response = httpx.get(base_url, params={'page': page}, headers=headers, timeout=10)
        response.raise_for_status()
        posts = response.json().get('posts', [])
        return [get_pic_info(post) for post in posts if get_pic_info(post)]
    except Exception as e:
        logger.error(f"Request error while fetching posts for page {page}: {e}")
        return []

def get_hot_pic_info(length, erotic=""):
    base_url = 'https://api.anime-pictures.net/api/v3/top'
    params = {
        'length': length,
        'erotic': erotic
    }
    try:
        response = httpx.get(base_url, params=params, headers=headers, timeout=10)
        if response.status_code == 200:
            top = response.json().get('top', [])
            return [get_pic_info(pic) for pic in top]
        else:
            logger.error(f"Failed to fetch hot pictures. Status code: {response.status_code}")
            return []
    except Exception as e:
        logger.error(f"Request error while fetching hot pictures: {e}")
        return []

def write_json(result, filename):
    result = sorted(result, key=lambda x: x.get('download_count', 0), reverse=True)
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=4)



def process_hot_pic(length, erotic, pic_dir, save_choice='both'):
    hot_pics = get_hot_pic_info(length, erotic)
    logger.info(f"Fetched {len(hot_pics)} hot pictures for length '{length}' and erotic '{erotic}'.")
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_data = [executor.submit(process_single_pic, data, pic_dir, save_choice=save_choice) for data in hot_pics]
        result_list = []
        for future in as_completed(future_to_data):
            try:
                future.result()  
            except Exception as e:
                logger.error(f"Error processing picture: {e}")
    write_json(result_list, f'{pic_dir}/hot_{length}_{erotic}.json')
    return hot_pics


def process_single_post(page, erotic, load_count, pic_dir, save_choice):
    posts = get_info_from_posts(page)
    filtered_posts = [
        pic for pic in posts if pic.get('download_count', 0) > load_count and pic.get('erotics', 0) == erotic
    ]
    logger.info(f"Page {page}: Found {len(filtered_posts)} posts with download_count > {load_count} and erotic={erotic}.")
    for pic in filtered_posts:
        if not pic:
            continue
        with httpx.Client() as client:
            try:
                if save_choice == 'both' or save_choice == 'small':
                    if os.path.exists(f'{pic_dir}/small/{pic["pic_id"]}.{pic["small_url"].split(".")[-1]}'):
                        logger.info(f"Small image for pic_id {pic['pic_id']} already exists. Skipping download.")
                        continue
                    save_pic(pic['small_url'], f'{pic_dir}/small', pic['pic_id'], client=client)
                    time.sleep(random.uniform(1, 2))  # To avoid overwhelming the server
                if save_choice == 'both' or save_choice == 'full':
                    if os.path.exists(f'{pic_dir}/full/{pic["pic_id"]}.{pic["url"].split(".")[-1]}'):
                        logger.info(f"Full image for pic_id {pic['pic_id']} already exists. Skipping download.")
                        continue
                    save_pic(pic['url'], f'{pic_dir}/full', pic['pic_id'], client=client)
                    time.sleep(random.uniform(8, 10))  # To avoid overwhelming the server
            except Exception as e:
                logger.error(f"Error processing picture: {e}")

    return filtered_posts

def process_post(erotic, load_count=100, pages=1, pic_dir='../pics', save_choice='both'):
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_data = {executor.submit(process_single_post, page, erotic, load_count, pic_dir, save_choice=save_choice): page for page in range(1, pages + 1)}
        result = []
        for future in future_to_data:
            try:
                post_info = future.result()
                if post_info:
                    result.extend(post_info)
                    logger.info(f"Processed page {future_to_data[future]} with {len(post_info)} posts.")
            except Exception as e:
                logger.error(f"Error processing post: {e}, page: {future_to_data[future]}")
    write_json(result, f'{pic_dir}/posts_{load_count}_{pages}.json')
    return result



length = 'day'  # Options: 'day', 'week'
erotic = 1  # Options: 0(non-erotic), 1 (erotic)
load_count = 10  # Minimum download count to filter posts
pages = 1  # Number of pages to process
# process_hot_pic(length=length, erotic=erotic, pic_dir=f'../pics/hot/{length}_{erotic}_{datetime.now().strftime("%m-%d")}', save_choice='full')
process_post(erotic=erotic, load_count=load_count, pages=pages, pic_dir=f'../pics/posts/{load_count}_{erotic}', save_choice='small')
