"""Transfer-function probe: pointer delta -> piece x mapping.

In a single hold, step the pointer +30px at a time and read the piece
element's x-offset vs the bg each step. slope = the widget's actual
thumb->piece ratio. Also solves 'what drag distance reaches gap_x'.
"""
import sys, time, random
sys.path.insert(0, "/Users/devin/repos/atria-keys")
from atria_keys.config import Config
from atria_keys.driver import BrowserDriver
from atria_keys.mailbox import build_reader
from atria_keys.challenge import (
    AlibabaCloudChallengeDriver, HANDLE_SELECTORS, TRACK_SELECTORS,
    BG_IMAGE_SELECTORS, PIECE_IMAGE_SELECTORS,
)

cfg = Config.load("config/atria.yaml")
run_id = "sweep" + str(int(time.time()))
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
    bg_el = ch._pick_opt(frame, BG_IMAGE_SELECTORS)
    piece_el = ch._pick_opt(frame, PIECE_IMAGE_SELECTORS)
    ibox = bg_el.bounding_box()
    hbox = handle.bounding_box()
    hx, hy = hbox["x"] + hbox["width"] / 2, hbox["y"] + hbox["height"] / 2

    print(f"bg css w={ibox['width']:.0f}  handle w={hbox['width']:.0f}", flush=True)
    pbox0 = piece_el.bounding_box()
    print(f"piece start off={pbox0['x'] - ibox['x']:.1f}", flush=True)

    page.mouse.move(hx, hy)
    page.wait_for_timeout(120)
    page.mouse.down()
    page.wait_for_timeout(180)

    total = 0
    for step in range(9):
        for _ in range(6):  # 6 × 5px moves
            total += 5
            page.mouse.move(hx + total, hy + random.uniform(-2, 2))
            page.wait_for_timeout(12)
        page.wait_for_timeout(300)
        pb = piece_el.bounding_box()
        px = pb["x"] - ibox["x"]
        print(f"delta={total}px -> piece_x={px:.1f}  ratio={px/total:.3f}", flush=True)
    page.mouse.up()
    page.wait_for_timeout(2500)
    print("verify:", [r.get("status") for r in driver.captured if "verify" in r.get("url", "")], flush=True)
finally:
    driver.__exit__(None, None, None)
