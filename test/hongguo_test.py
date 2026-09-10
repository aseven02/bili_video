import httpx
import json


url = 'https://hongguoduanju.com/rank/hot-real-drama'
params = {
    '__loader': 'rank_hot-real-drama/page',
    '__ssrDirect': 'true'
}

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
}

response = httpx.get(url, params=params, headers=headers)
response.raise_for_status()  # Raise an exception for HTTP errors
data = next(
    json.loads(line.removeprefix('data:'))
    for line in response.text.splitlines()
    if line.startswith('data:')
)['content']

print(len(data))
print(data.keys())

with open('hongguo_test.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=4)