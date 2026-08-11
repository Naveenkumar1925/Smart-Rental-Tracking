import sys
import cv2
import numpy as np
import torch
import torch.nn.functional as F
import torchvision.transforms as T
from torchvision.models import vgg16, VGG16_Weights
from torchvision.models.segmentation import (
    deeplabv3_resnet101, DeepLabV3_ResNet101_Weights,
)
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD = [0.229, 0.224, 0.225]


_VOC_VEHICLE_IDS = {6, 7, 14, 19}  


def load_models():
    seg = deeplabv3_resnet101(
        weights=DeepLabV3_ResNet101_Weights.DEFAULT).eval().to(DEVICE)
    # VGG16 feature extractor; we tap a few intermediate layers.
    vgg = vgg16(weights=VGG16_Weights.DEFAULT).features.eval().to(DEVICE)
    for p in vgg.parameters():
        p.requires_grad_(False)
    for p in seg.parameters():
        p.requires_grad_(False)
    return seg, vgg


def segment_rembg(img_bgr):
   
    from rembg import remove
    from PIL import Image
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    out = remove(Image.fromarray(rgb))     
    alpha = np.array(out)[:, :, 3]
    mask = ((alpha > 127) * 255).astype(np.uint8)
    # keep only the largest blob (the truck), drop stray specks
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n > 1:
        biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = ((lab == biggest) * 255).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    return mask


def segment_vehicle(img_bgr, seg_model, fallback_center=True):
    """Return uint8 0/255 mask of the machine using pretrained DeepLabV3."""
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    tf = T.Compose([T.ToTensor(),
                    T.Normalize(IMAGENET_MEAN, IMAGENET_STD)])
    x = tf(rgb).unsqueeze(0).to(DEVICE)
    with torch.no_grad():
        out = seg_model(x)["out"][0]             
    pred = out.argmax(0).cpu().numpy().astype(np.int32)

    mask = np.isin(pred, list(_VOC_VEHICLE_IDS)).astype(np.uint8) * 255
  
    if mask.sum() == 0:
        mask = ((pred != 0) * 255).astype(np.uint8)

    if mask.sum() == 0 and fallback_center:
        h, w = pred.shape
        mask = np.zeros((h, w), np.uint8)
        mask[int(h*0.1):int(h*0.9), int(w*0.1):int(w*0.9)] = 255

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1)
    return mask


def align(after_bgr, before_bgr, mask=None, min_matches=15):
    g_a = cv2.cvtColor(after_bgr, cv2.COLOR_BGR2GRAY)
    g_b = cv2.cvtColor(before_bgr, cv2.COLOR_BGR2GRAY)
    orb = cv2.ORB_create(8000)        
    ka, da = orb.detectAndCompute(g_a, mask)
    kb, db = orb.detectAndCompute(g_b, mask)
    if da is None or db is None:
        return after_bgr, False, 0
    raw = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [m for m, n in (p for p in raw if len(p) == 2)
            if m.distance < 0.80 * n.distance]     
    if len(good) < min_matches:
        return after_bgr, False, len(good)
    src = np.float32([ka[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kb[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if H is None:
        return after_bgr, False, len(good)
    h, w = before_bgr.shape[:2]
    return cv2.warpPerspective(after_bgr, H, (w, h)), True, int(inl.sum())


_TAP_LAYERS = [3, 8, 15, 22]


def _to_tensor(img_bgr):
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    tf = T.Compose([T.ToTensor(),
                    T.Normalize(IMAGENET_MEAN, IMAGENET_STD)])
    return tf(rgb).unsqueeze(0).to(DEVICE)


def _feature_maps(vgg, x):
    feats, h = [], x
    for i, layer in enumerate(vgg):
        h = layer(h)
        if i in _TAP_LAYERS:
            feats.append(h)
        if i >= _TAP_LAYERS[-1]:
            break
    return feats


def deep_diff(before_bgr, after_bgr, vgg):
    """Perceptual difference heatmap in [0,1], size of the input image."""
    Hh, Ww = before_bgr.shape[:2]
    with torch.no_grad():
        fb = _feature_maps(vgg, _to_tensor(before_bgr))
        fa = _feature_maps(vgg, _to_tensor(after_bgr))
        acc = torch.zeros(1, 1, Hh, Ww, device=DEVICE)
        for a, b in zip(fa, fb):
          
            a = F.normalize(a, dim=1)
            b = F.normalize(b, dim=1)
            d = ((a - b) ** 2).sum(1, keepdim=True)     
            d = F.interpolate(d, size=(Hh, Ww),
                              mode="bilinear", align_corners=False)
            acc = acc + d
    heat = acc[0, 0].cpu().numpy()
    heat = (heat - heat.min()) / (np.ptp(heat) + 1e-8)   
    return heat


def localize(heat, mask, thresh=0.35, min_area_frac=0.0005):
    m = (mask > 0).astype(np.uint8)
    m = cv2.erode(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)), 1)
    heat = heat * m
    binmap = (heat > thresh).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    binmap = cv2.morphologyEx(binmap, cv2.MORPH_OPEN, k, 1)
    binmap = cv2.morphologyEx(binmap, cv2.MORPH_CLOSE, k, 2)

    veh_area = max(int(m.sum()), 1)
    min_area = min_area_frac * veh_area
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binmap, 8)
    boxes = []
    damaged_pixels = 0
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < min_area:
            continue
        damaged_pixels += int(area)         
        score = float(heat[y:y+h, x:x+w].mean())
        boxes.append((int(x), int(y), int(w), int(h), round(score, 3)))
    boxes.sort(key=lambda b: b[4], reverse=True)
    damage_pct = 100.0 * damaged_pixels / veh_area
    return boxes, round(damage_pct, 2)


def annotate(img_bgr, boxes):
    out = img_bgr.copy()
    for (x, y, w, h, s) in boxes:
        cv2.rectangle(out, (x, y), (x+w, y+h), (0, 0, 255), 2)
        cv2.putText(out, f"{s:.2f}", (x, max(0, y-6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, cv2.LINE_AA)
    return out


def run(before_path, after_path, thresh=0.35, min_matches=15, segmenter="deeplab"):
    before = cv2.imread(before_path)
    after = cv2.imread(after_path)
    if before is None or after is None:
        raise FileNotFoundError("Could not read an image.")
    if after.shape[:2] != before.shape[:2]:
        after = cv2.resize(after, (before.shape[1], before.shape[0]))

    seg, vgg = load_models()
    if segmenter == "none":
        mask = np.full(before.shape[:2], 255, np.uint8)
    elif segmenter == "rembg":
        mask = segment_rembg(before)
    else:
        mask = segment_vehicle(before, seg)
    aligned, ok, nmatch = align(after, before, mask=mask, min_matches=min_matches)
    if not ok:
        return {"ok": False,
                "reason": f"alignment failed ({nmatch} matches) — re-shoot"}

    heat = deep_diff(before, aligned, vgg)
    boxes, damage_pct = localize(heat, mask, thresh=thresh)
    return {"ok": True, "matches": nmatch, "boxes": boxes,
            "damage_pct": damage_pct,
            "annotated": annotate(aligned, boxes),
            "heatmap": (heat * 255).astype(np.uint8), "mask": mask}


BEFORE_IMAGE = r"A:\Catterpillar\crane\front\before.jpg"
AFTER_IMAGE  = r"crane\front\fee78940-3a7f-4cb3-87c8-4aa1a8f8eebb.jpg"

OUTPUT_ANNOTATED = r"annotated_after.png"   
OUTPUT_HEATMAP   = r"heatmap.png"

THRESHOLD = 0.35   
SEGMENTER = "rembg"

MIN_MATCHES = 4



if __name__ == "__main__":
    import os

   
    if len(sys.argv) >= 3:
        before_path, after_path = sys.argv[1], sys.argv[2]
        thresh = float(sys.argv[3]) if len(sys.argv) > 3 else THRESHOLD
    else:
        before_path, after_path, thresh = BEFORE_IMAGE, AFTER_IMAGE, THRESHOLD

    for tag, p in (("BEFORE", before_path), ("AFTER", after_path)):
        if not os.path.exists(p):
            print(f"[ERROR] {tag} image not found:\n    {p}")
            print("Fix the path in the CONFIG block at the top of the script.")
            sys.exit(1)

    print(f"BEFORE : {before_path}")
    print(f"AFTER  : {after_path}")
    print(f"Device : {DEVICE}   (cuda = GPU in use)")
    print("Loading pretrained models (first run downloads weights)...")

    r = run(before_path, after_path, thresh=thresh,
            min_matches=MIN_MATCHES, segmenter=SEGMENTER)
    if not r["ok"]:
        print("REJECTED:", r["reason"])
        sys.exit(0)

    pct = r["damage_pct"]
    if pct < 1:
        level = "MINIMAL"
    elif pct < 5:
        level = "MINOR"
    elif pct < 15:
        level = "MODERATE"
    else:
        level = "SEVERE"

    print("\n" + "=" * 44)
    print(f"  DAMAGE: {pct:.2f}% of vehicle surface   [{level}]")
    print(f"  Regions detected: {len(r['boxes'])}")
    print("=" * 44)
    for b in r["boxes"]:
        print("  (x, y, w, h, score) =", b)

    banner = f"Damage: {pct:.2f}%  ({level})"
    cv2.rectangle(r["annotated"], (0, 0), (360, 40), (0, 0, 0), -1)
    cv2.putText(r["annotated"], banner, (10, 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)

    cv2.imwrite(OUTPUT_ANNOTATED, r["annotated"])
    cv2.imwrite(OUTPUT_HEATMAP, cv2.applyColorMap(r["heatmap"], cv2.COLORMAP_JET))
    print(f"\nwrote:\n  {os.path.abspath(OUTPUT_ANNOTATED)}\n  {os.path.abspath(OUTPUT_HEATMAP)}")