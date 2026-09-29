"""Visual-verification probe: does the dragged piece land on the gap?

Runs the real live widget, drags with the harness trajectory, and while
still holding measures the piece element's x-offset vs the detected gap.
Solver is exonerated iff |piece_x - gap_x| <= ~3px mid-hold.
"""
import sys, time, random
sys.path.insert(0, "/Users/devin/repos/atria-keys")
from atria_keys.config import Config
from atria_keys.driver import BrowserDriver
from atria_keys.mailbox import build_reader
from atria_keys.challenge import (
    AlibabaCloudChallengeDriver, HANDLE_SELECTORS, TRACK_SELECTORS,
    BG_IMAGE_SELECTORS, PIECE_IMAGE_SELECTORS,
    generate_slide_path, generate_approach_path,
)

cfg = Config.load("config/atria.yaml")
run_id = "probe" + str(int(time.time()))
mb = build_reader(cfg)
email = mb.allocate_address(run_id)
print("email:", email, flush=True)

driver = BrowserDriver(cfg, run_id)
driver.__enter__()
page = driver.page
try:
    driver.fill_registration(email)
    page.wait_for_timeout(2500)

    ch = AlibabaCloudChallengeDriver("keys/artifacts", captured_token_fn=driver.captured_token)
    frame = ch._find_widget_frame(page)
    ch._open_widget(page, frame)
    page.wait_for_timeout(1200)

    handle = ch._pick(frame, HANDLE_SELECTORS, "handle")
    track = ch._pick(frame, TRACK_SELECTORS, "track")
    bg_el = ch._pick_opt(frame, BG_IMAGE_SELECTORS)
    piece_el = ch._pick_opt(frame, PIECE_IMAGE_SELECTORS)
    panel = frame.query_selector("#aliyunCaptcha-window-float")
    from pathlib import Path
    out = Path("keys/probe")
    out.mkdir(parents=True, exist_ok=True)
    if panel:
        panel.screenshot(path=str(out / "panel_before.png"))
        print("panel_before.png saved", flush=True)

    distance, conf, method = ch.resolve_distance(page, frame, track)
    print(f"distance={distance:.1f} css px, conf={conf:.2f}, method={method}", flush=True)
    from pathlib import Path
    out = Path("keys/probe")
    out.mkdir(parents=True, exist_ok=True)
    if ch._last_puzzle:
        (out / "bg.png").write_bytes(ch._last_puzzle["bg"])
        if ch._last_puzzle.get("piece"):
            (out / "piece.png").write_bytes(ch._last_puzzle["piece"])
        print("puzzle imgs -> keys/probe/", flush=True)

    ibox = bg_el.bounding_box()
    pbox0 = piece_el.bounding_box()
    start_off = pbox0["x"] - ibox["x"]
    print(f"piece start offset: {start_off:.1f}px, bg css width: {ibox['width']:.1f}", flush=True)

    # Full harness drag (open-loop bezier + closed-loop servo) then measure
    # where the piece actually landed vs the detected gap.
    dist2, method2 = ch._drag(page, frame, handle, track)
    # The probe's own resolve above may have captured an unpainted element;
    # _drag re-resolves internally — dump what IT used.
    if ch._last_puzzle:
        (out / "bg2.png").write_bytes(ch._last_puzzle["bg"])
        if ch._last_puzzle.get("piece"):
            (out / "piece2.png").write_bytes(ch._last_puzzle["piece"])
        print("post-drag imgs -> bg2/piece2", flush=True)
    if panel:
        panel.screenshot(path=str(out / "panel_after.png"))
        print("panel_after.png saved", flush=True)
    gap_css = distance + start_off
    for delay in (0, 400, 1000):
        if delay:
            page.wait_for_timeout(delay)
        pbox1 = piece_el.bounding_box() if piece_el else None
        if not pbox1 or not ibox:
            print(f"piece x @+{delay}ms: element gone (widget re-rendered)", flush=True)
            continue
        actual = pbox1["x"] - ibox["x"]
        print(f"piece x @+{delay}ms: {actual:.1f}  target: {gap_css:.1f}  err: {actual - gap_css:+.1f}px", flush=True)
    page.screenshot(path="keys/probe_hold.png")
    page.wait_for_timeout(200)
    page.wait_for_timeout(4000)
    print("captcha-ish responses:", flush=True)
    for r in driver.captured:
        u = r.get("url", "")
        if "captcha" in u or "verify" in u:
            print(" ", r.get("status"), u[:140], flush=True)
finally:
    driver.__exit__(None, None, None)

# offline dump happens above; here just a marker
