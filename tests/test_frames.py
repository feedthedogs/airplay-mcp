import numpy as np
import pytest

from airplay_mcp.frames import ChangeConfig, FrameStore
from airplay_mcp.receivers.base import ReceiverStatus
from airplay_mcp.service import ScreenService

W, H = 886, 1920  # typical portrait iPhone mirroring stream


def blank() -> np.ndarray:
    return np.full((H, W, 3), 240, dtype=np.uint8)


def with_box(img: np.ndarray, x: int, y: int, w: int, h: int, value: int = 20) -> np.ndarray:
    img = img.copy()
    img[y : y + h, x : x + w] = value
    return img


@pytest.fixture
def store() -> FrameStore:
    return FrameStore(ChangeConfig())


@pytest.fixture
def service(store: FrameStore) -> ScreenService:
    return ScreenService(store, lambda: ReceiverStatus(True, "AI Screen"), lambda: True)


def test_first_frame_gets_seq(store: FrameStore) -> None:
    assert store.submit(blank())
    assert store.seq == 1


def test_identical_frame_is_not_a_change(store: FrameStore) -> None:
    store.submit(blank())
    assert not store.submit(blank())
    assert store.seq == 1


def test_status_bar_clock_is_ignored(store: FrameStore) -> None:
    store.submit(blank())
    assert not store.submit(with_box(blank(), 40, 20, 120, 50))  # clock digits in top 6%
    assert store.seq == 1


def test_small_digit_counts_as_change(store: FrameStore) -> None:
    store.submit(blank())
    # A Sudoku digit: ~28x40px stroke box in the middle of the screen.
    assert store.submit(with_box(blank(), 400, 900, 28, 40))
    assert store.seq == 2


def test_changed_bbox_locates_change(store: FrameStore) -> None:
    store.submit(blank())
    store.submit(with_box(blank(), 400, 900, 28, 40))
    x, y, w, h = store.changed_bbox(1)
    assert x <= 400 / W <= x + w and x <= 428 / W <= x + w
    assert y <= 900 / H <= y + h and y <= 940 / H <= y + h
    assert w < 0.15 and h < 0.1


def test_static_screen_still_returns_image(service: ScreenService, store: FrameStore) -> None:
    store.submit(blank())
    first = service.get_screen("a", settle_ms=0)
    assert first.image is not None and first.text.startswith("frame 1")
    again = service.get_screen("a", settle_ms=0)  # model may have lost the earlier image
    assert again.image is not None
    held = service.get_screen("a", settle_ms=0, since_frame=1)
    assert held.image is None and "unchanged" in held.text
    forced = service.get_screen("a", settle_ms=0, since_frame=1, force=True)
    assert forced.image is not None


def test_changes_mode_returns_small_crop(service: ScreenService, store: FrameStore) -> None:
    store.submit(blank())
    service.get_screen("a", settle_ms=0)
    store.submit(with_box(blank(), 400, 900, 28, 40))
    res = service.get_screen("a", mode="changes", settle_ms=0)
    assert res.image is not None
    assert "changes since frame 1" in res.text
    full = service.get_screen("a", settle_ms=0, force=True)
    tokens = lambda r: int(r.text.split("(~")[1].split(" ")[0])  # noqa: E731
    assert tokens(res) * 5 < tokens(full)


def test_default_image_is_right_sized(service: ScreenService, store: FrameStore) -> None:
    store.submit(blank())
    res = service.get_screen("a", settle_ms=0)
    assert "image 473x1024" in res.text
    assert "(~646 tokens)" in res.text


def test_letterbox_is_cropped(service: ScreenService, store: FrameStore) -> None:
    img = np.zeros((1080, 1920, 3), dtype=np.uint8)  # portrait phone pillarboxed in 16:9
    img[:, 710:1210] = 200
    store.submit(img)
    res = service.get_screen("a", settle_ms=0)
    assert "screen 500x1080" in res.text


def test_region_zoom_bypasses_dedupe(service: ScreenService, store: FrameStore) -> None:
    store.submit(blank())
    service.get_screen("a", settle_ms=0)
    res = service.get_screen("a", settle_ms=0, region=(0, 0.25, 1, 0.5))
    assert res.image is not None and "region x=0.000 y=0.250" in res.text


def test_wait_for_change_times_out_and_fires(service: ScreenService, store: FrameStore) -> None:
    store.submit(blank())
    service.get_screen("a", settle_ms=0)
    assert "no change" in service.wait_for_change("a", timeout_s=0.2)
    store.submit(with_box(blank(), 100, 500, 200, 200))
    assert service.wait_for_change("a", timeout_s=0.2).startswith("changed")


def test_no_screen_message(service: ScreenService) -> None:
    res = service.get_screen("a", settle_ms=0)
    assert res.image is None and "Screen Mirroring" in res.text


def test_connected_but_undecodable_is_reported(store: FrameStore) -> None:
    st = ReceiverStatus(True, "AI Screen", "mirroring", client="iPhone (iPhone17,3)")
    svc = ScreenService(store, lambda: st, lambda: True, video_stats=lambda: (430_080, 0))
    status = svc.status(object())
    assert "video: 420 KB received, 0 frames decoded" in status
    text = svc.wait_for_change(object(), timeout_s=0.01)
    assert "iPhone (iPhone17,3) is connected but no picture has decoded" in text
