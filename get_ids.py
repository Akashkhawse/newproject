import re
with open("static/js/dashboard.js") as f:
    js = f.read()

ids = re.findall(r'getElementById\((["\'])(.*?)\1\)', js)
ids = sorted(set(v for _, v in ids))
for id_ in ids:
    print(id_)
