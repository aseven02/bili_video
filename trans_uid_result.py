"""提取并保存uid结果的核心信息，为视频详细信息爬取的代码提供输入"""

import json
import os


file_dir = "creator_batches_result"
video_dir = "video_batches"
os.makedirs(video_dir, exist_ok=True)

for file in os.listdir(file_dir):
    if file.endswith(".json"):
        batch_num = file.split(".")[0].split("h")[-1]
        video_batches = []
        with open(os.path.join(file_dir, file), "r", encoding="utf-8") as f:
            data = json.load(f)
            for creator in data.get('creators', []):
                if creator.get('uid') and creator.get("videos"):
                    video_batches.append({
                        'uid': creator['uid'],
                        'videos': [video['bvid'] for video in creator['videos'] if video.get('bvid')]
                    })
        print(f"Batch {batch_num}: {len(video_batches)} creators with {sum(len(v['videos']) for v in video_batches)} videos found.")
        # with open(os.path.join(video_dir, f"video_batches_{batch_num}.json"), "w", encoding="utf-8") as f:
        #     json.dump(video_batches, f, ensure_ascii=False, indent=4)

                    
