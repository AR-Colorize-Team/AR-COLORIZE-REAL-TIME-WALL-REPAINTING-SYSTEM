import json, sys
cols = ["wall", "ceiling", "floor", "side_wall", "window", "curtain", "objects", "iou", "spill", "bf1", "walls_after"]
for app in sys.argv[1:]:
    d = json.load(open(f"results_{app}.json"))
    print(f"\n== {app}")
    print(f"{'scenario':18s}" + "".join(f"{c[:9]:>10s}" for c in cols) + "  toast")
    for k, v in d.items():
        print(f"{k:18s}" + "".join(f"{(v.get(c) if v.get(c) is not None else float('nan')):10.3f}" for c in cols) + "  " + "; ".join(v["toasts"])[:70] + ("  ERR " + v["errors"][0][:80] if v["errors"] else ""))
