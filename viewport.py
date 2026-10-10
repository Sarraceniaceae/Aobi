"""Locate the game canvas from image features; no window-border assumptions."""
from dataclasses import dataclass
from pathlib import Path
import ctypes

import cv2
import numpy as np
from PIL import Image

W, H = 1071, 627


def enable_dpi_awareness():
    try:
        user = ctypes.windll.user32
        user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
        user.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


class ViewportNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class Mapping:
    sx: float
    sy: float
    tx: float
    ty: float
    reference: str
    inliers: int

    def screen_point(self, point, origin):
        return (round(origin[0]+self.tx+self.sx*point[0]),
                round(origin[1]+self.ty+self.sy*point[1]))

    def normalize(self, rgb):
        matrix = np.array([[self.sx, 0, self.tx], [0, self.sy, self.ty]], np.float32)
        return cv2.warpAffine(rgb, matrix, (W, H), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)

    def info(self):
        return {"width": round(self.sx*W), "height": round(self.sy*H),
                "scale_x": round(self.sx, 3), "scale_y": round(self.sy, 3),
                "reference": self.reference, "inliers": self.inliers}


class ViewportLocator:
    def __init__(self, assets=None):
        assets = assets or Path(__file__).resolve().parent/"assets"
        self.sift = cv2.SIFT_create(nfeatures=2200, contrastThreshold=.035)
        self.matcher = cv2.BFMatcher(cv2.NORM_L2)
        self.references = []
        for name in ("arena", "lobby", "selection"):
            gray = np.asarray(Image.open(assets/f"viewport_{name}.png").convert("L"))
            keys, descriptors = self.sift.detectAndCompute(gray, None)
            self.references.append((name, np.float32([k.pt for k in keys]), descriptors))
        self.mapping = None
        self.shape = None
        self.anchors = []
        self.revision = 0

    def invalidate(self):
        self.mapping = None
        self.anchors = []

    @staticmethod
    def _spread(points):
        return len(points) >= 8 and np.ptp(points[:, 0]) > W*.60 and np.ptp(points[:, 1]) > H*.55

    def _remember(self, rgb):
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        self.anchors = []
        for y in np.linspace(42, H-42, 5).astype(int):
            for x in np.linspace(42, W-42, 6).astype(int):
                patch = gray[y-18:y+19, x-18:x+19].copy()
                if patch.std() > 12:
                    self.anchors.append((x, y, patch))

    def _cached_valid(self, rgb):
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        matched = []
        for x, y, patch in self.anchors:
            score = cv2.minMaxLoc(cv2.matchTemplate(gray[y-20:y+21, x-20:x+21], patch,
                                                    cv2.TM_CCOEFF_NORMED))[1]
            if score > .91:
                matched.append((x, y))
        return self._spread(np.asarray(matched))

    def locate(self, rgb):
        if self.mapping and self.shape == rgb.shape:
            normalized = self.mapping.normalize(rgb)
            if self._cached_valid(normalized):
                return normalized
        self.invalidate()
        height, width = rgb.shape[:2]
        reduction = min(1., 1600/max(height, width))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        if reduction < 1:
            gray = cv2.resize(gray, None, fx=reduction, fy=reduction, interpolation=cv2.INTER_AREA)
        keys, descriptors = self.sift.detectAndCompute(gray, None)
        candidates = []
        if descriptors is not None and len(keys) >= 24:
            live = np.float32([k.pt for k in keys])/reduction
            for name, ref_points, ref_desc in self.references:
                pairs = self.matcher.knnMatch(ref_desc, descriptors, k=2)
                good = [a for pair in pairs if len(pair) == 2 for a, b in [pair] if a.distance < .70*b.distance]
                # One live feature must not count repeatedly toward geometric confidence.
                unique = {}
                for match in sorted(good, key=lambda m:m.distance):
                    unique.setdefault(match.trainIdx, match)
                good = list(unique.values())
                if len(good) < 24:
                    continue
                src = ref_points[[m.queryIdx for m in good]]
                dst = live[[m.trainIdx for m in good]]
                affine, mask = cv2.estimateAffine2D(src, dst, method=cv2.RANSAC,
                    ransacReprojThreshold=3/reduction, maxIters=1500, confidence=.995)
                if affine is None:
                    continue
                sx, sy = affine[0, 0], affine[1, 1]
                if min(sx, sy) < .695 or max(sx, sy) > 4.5 or not .75 < sx/sy < 1.34:
                    continue
                if abs(affine[0, 1]) > .015*sx or abs(affine[1, 0]) > .015*sy:
                    continue
                inside = mask.ravel().astype(bool)
                src, dst = src[inside], dst[inside]
                if len(src) < 24 or not self._spread(src):
                    continue
                sx, tx = np.linalg.lstsq(np.c_[src[:, 0], np.ones(len(src))], dst[:, 0], rcond=None)[0]
                sy, ty = np.linalg.lstsq(np.c_[src[:, 1], np.ones(len(src))], dst[:, 1], rcond=None)[0]
                error = np.linalg.norm(dst-src*[sx, sy]-[tx, ty], axis=1)
                if np.quantile(error, .9) > 2.5*max(sx, sy):
                    continue
                # Require the entire canvas. A partially off-screen window cannot be controlled safely.
                if tx < -2 or ty < -2 or tx+sx*W > width+2 or ty+sy*H > height+2:
                    continue
                candidates.append(Mapping(float(sx), float(sy), float(tx), float(ty), name, len(src)))
        if not candidates:
            raise ViewportNotFound("未能定位完整游戏画面，请显示学院或修炼场，并放大过小的窗口、移除遮挡。")
        best = max(candidates, key=lambda m:m.inliers)
        for other in candidates:
            if other.inliers >= best.inliers*.65 and (abs(other.tx-best.tx) > 10 or abs(other.ty-best.ty) > 10):
                raise ViewportNotFound("发现多个可能的游戏区域，等待画面明确后自动重试。")
        self.mapping = best
        self.shape = rgb.shape
        normalized = best.normalize(rgb)
        self._remember(normalized)
        self.revision += 1
        return normalized
