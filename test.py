import json
from collections import defaultdict
file_path = 'bili_creator_videos.json'

with open (file_path, 'r', encoding='utf-8') as file:
    data = json.load(file)

creators = data.get('creators', [])

counts = defaultdict(list)
total_videos = 0

for creator in creators:
    uid = creator.get('uid')
    videos = creator.get('videos', [])
    # print(f"Creator UID: {uid}")
    total_videos += len(videos)
    for video in videos:
        if video.get('is_union_video'):
            counts[uid].append(video.get('bvid'))

print('total_creators:', len(creators))
print('total_videos:', total_videos)
print('len_union_creators:', len(counts))
print('union_videos:', sum(len(v) for v in counts.values()))
print('union_creators:')
for uid, bvids in counts.items():
    print(f"  UID: {uid}, Union Videos: {len(bvids)}")