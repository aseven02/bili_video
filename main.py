import json
import os
with open('batch1.json', 'r', encoding='utf-8') as f:
    all_uid = json.load(f)

print(len(all_uid['creators']))