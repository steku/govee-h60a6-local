#!/usr/bin/env python3
"""Interactive visual test & protocol verification script for Govee H60A6.

Run against a live H60A6 to visually confirm:
  1. Core LAN controls (status, power, brightness, RGB, Kelvin 2200K-6500K)
  2. Whether the firmware accepts `ptReal` over Wi-Fi LAN UDP 4003 (or BLE)
  3. Exact Ambient Ring / Dual-Zone segment packet format
  4. Individual segment walk (segments 1..12 + bit 13) and segment brightness
  5. Razer/DreamView multi-segment streaming

Examples:
  python3 tools/visual_test.py --host 192.168.6.224
  python3 tools/visual_test.py --host 192.168.6.224 --only ring
  python3 tools/visual_test.py --host 192.168.6.224 --only segments
  python3 tools/visual_test.py --ble AA:BB:CC:DD:EE:FF --mode ble_only --only ring
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
from typing import Any, Awaitable, Callable

COMPONENT_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "govee_h60a6_local"
)


def _load_core_modules() -> tuple[Any, Any, Any]:
    pkg_name = "_govee_visual_test_core"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
        sys.modules[pkg_name] = pkg

    loaded = []
    for mod_short in ("const", "protocol", "transport"):
        full_name = f"{pkg_name}.{mod_short}"
        if full_name in sys.modules:
            loaded.append(sys.modules[full_name])
            continue
        spec = importlib.util.spec_from_file_location(
            full_name, COMPONENT_DIR / f"{mod_short}.py"
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load {mod_short}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded[0], loaded[1], loaded[2]


const, protocol, transport = _load_core_modules()


class VisualTestRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.results: list[tuple[str, str, str]] = []
        self.hub = transport.GoveeLanHub(listen_port=args.listen_port)
        self.ble = transport.GoveeBleTransport(args.ble) if args.ble else None
        self.client = transport.GoveeH60A6Client(
            host=args.host if args.mode != const.TRANSPORT_BLE_ONLY else None,
            hub=self.hub if args.mode != const.TRANSPORT_BLE_ONLY else None,
            ble_transport=self.ble,
            transport_mode=args.mode,
            command_delay_ms=100,
            lan_timeout=args.timeout,
        )

    async def run_step(
        self,
        step_id: str,
        title: str,
        expect: str,
        action: Callable[[], Awaitable[str]],
    ) -> bool:
        """Execute one visual test step and collect y/n confirmation."""
        print(f"\n[{step_id}] {title}")
        print(f"  Expected: {expect}")
        while True:
            try:
                detail = await action()
                print(f"  Sent:     {detail}")
            except Exception as err:
                print(f"  ERROR:    {err}")
                self.results.append((step_id, "ERROR", f"{title} ({err})"))
                return False

            if self.args.auto:
                await asyncio.sleep(self.args.delay)
                self.results.append((step_id, "SENT", title))
                return True

            ans = input("  Did you see this? [y=yes / n=no / r=repeat / s=skip-section / q=quit]: ").strip().lower()
            if ans == "r":
                continue
            if ans == "q":
                self.results.append((step_id, "ABORTED", title))
                raise KeyboardInterrupt
            if ans == "s":
                self.results.append((step_id, "SKIPPED", title))
                return False
            if ans in ("y", "yes", ""):
                note = ""
                self.results.append((step_id, "PASS", f"{title}{note}"))
                return True
            note = input("  Optional note (what actually happened, Enter to skip): ").strip()
            self.results.append((step_id, "FAIL", f"{title}" + (f" -> {note}" if note else "")))
            return True

    async def test_core(self) -> None:
        print("\n=== SECTION 1: CORE LAN / BLE CONTROLS ===")
        if self.client.wifi_enabled:
            await self.hub.async_start()
            print(
                f"UDP hub bound on port {self.hub.bound_port} "
                f"(receive_enabled={self.hub.receive_enabled})"
            )
            if self.hub.receive_enabled:
                devs = await self.hub.async_discover(
                    timeout=1.5, unicast_host=self.args.host
                )
                for d in devs:
                    print(
                        f"  Scan reply: sku={d.sku} ip={d.ip} id={d.device_id} "
                        f"wifi={d.wifi_version_soft} ble={d.ble_version_soft}"
                    )
                try:
                    st = await self.client.async_get_status()
                    print(f"  Initial devStatus: {st}")
                except Exception as err:
                    print(f"  Initial devStatus failed: {err}")

        steps: list[tuple[str, str, str, Callable[[], Awaitable[str]]]] = [
            (
                "C1",
                "Power OFF",
                "Entire ceiling light turns OFF",
                lambda: self._wrap(self.client.async_turn(False), "turn=0"),
            ),
            (
                "C2",
                "Power ON",
                "Ceiling light turns ON",
                lambda: self._wrap(self.client.async_turn(True), "turn=1"),
            ),
            (
                "C3",
                "Brightness 100%",
                "Light jumps to full brightness (100%)",
                lambda: self._wrap(self.client.async_set_brightness(100), "brightness=100"),
            ),
            (
                "C4",
                "Brightness 15%",
                "Light dims noticeably to 15%",
                lambda: self._wrap(self.client.async_set_brightness(15), "brightness=15"),
            ),
            (
                "C5",
                "Brightness 80%",
                "Light returns to 80% brightness",
                lambda: self._wrap(self.client.async_set_brightness(80), "brightness=80"),
            ),
            (
                "C6",
                "Solid Color RED (colorwc)",
                "Light turns solid RED (255, 0, 0)",
                lambda: self._wrap(self.client.async_set_rgb(255, 0, 0), "colorwc=(255,0,0)"),
            ),
            (
                "C7",
                "Solid Color GREEN (colorwc)",
                "Light turns solid GREEN (0, 255, 0)",
                lambda: self._wrap(self.client.async_set_rgb(0, 255, 0), "colorwc=(0,255,0)"),
            ),
            (
                "C8",
                "Solid Color BLUE (colorwc)",
                "Light turns solid BLUE (0, 0, 255)",
                lambda: self._wrap(self.client.async_set_rgb(0, 0, 255), "colorwc=(0,0,255)"),
            ),
            (
                "C9",
                "Warm White 2200K (colorwc)",
                "Light switches to very warm white (2200K)",
                lambda: self._wrap(self.client.async_set_color_temp(2200), "colorwc=2200K"),
            ),
            (
                "C10",
                "Cool White 6500K (colorwc)",
                "Light switches to crisp cool daylight (6500K)",
                lambda: self._wrap(self.client.async_set_color_temp(6500), "colorwc=6500K"),
            ),
        ]
        for step_id, title, expect, fn in steps:
            cont = await self.run_step(step_id, title, expect, fn)
            if not cont:
                break

    async def test_ring_protocols(self) -> None:
        print("\n=== SECTION 2: DUAL-ZONE & AMBIENT RING VERIFICATION ===")
        print(
            "Tests verified H60A6 `33 05 15 01` dual-zone bitmask control\n"
            "(0x0FFF = 12 ring segments, 0x1000 = main panel, 0x1FFF = full fixture)."
        )
        # Put light in a neutral warm white baseline first
        await self.client.async_turn(True)
        await self.client.async_set_brightness(80)
        await self.client.async_set_color_temp(3000)

        steps: list[tuple[str, str, str, Callable[[], Awaitable[str]]]] = [
            (
                "R1",
                "Ambient Ring (`0x0FFF`) -> RED 100%",
                "Ambient ring turns RED while main panel stays warm white",
                lambda: self._wrap(
                    self.client.async_set_segment_color(
                        255, 0, 0, const.ALL_RING_SEGMENTS_MASK, brightness_pct=100
                    ),
                    "ring=RED@100%",
                ),
            ),
            (
                "R2",
                "Ambient Ring (`0x0FFF`) -> BLUE 25%",
                "Ambient ring dims to low BLUE while main panel stays unchanged",
                lambda: self._wrap(
                    self.client.async_set_segment_color(
                        0, 0, 255, const.ALL_RING_SEGMENTS_MASK, brightness_pct=25
                    ),
                    "ring=BLUE@25%",
                ),
            ),
            (
                "R3",
                "Ambient Ring (`0x0FFF`) -> GREEN 100%",
                "Ambient ring brightens to GREEN",
                lambda: self._wrap(
                    self.client.async_set_segment_color(
                        0, 255, 0, const.ALL_RING_SEGMENTS_MASK, brightness_pct=100
                    ),
                    "ring=GREEN@100%",
                ),
            ),
            (
                "R4",
                "Main Panel ONLY (`0x1000`) -> MAGENTA 100%",
                "Main panel turns MAGENTA while ambient ring stays GREEN",
                lambda: self._wrap(
                    self.client.async_set_segment_color(
                        255, 0, 255, const.MAIN_LIGHT_SEGMENT_MASK, brightness_pct=100
                    ),
                    "main=MAGENTA@100%",
                ),
            ),
            (
                "R5",
                "Ambient Ring OFF (`0x0FFF` -> 0%)",
                "Ambient ring turns OFF while main panel stays MAGENTA",
                lambda: self._wrap(
                    self.client.async_set_segment_brightness(
                        0, const.ALL_RING_SEGMENTS_MASK
                    ),
                    "ring=OFF",
                ),
            ),
            (
                "R6",
                "Full Fixture (`0x1FFF`) -> ORANGE 100%",
                "Both main panel and ambient ring turn ORANGE",
                lambda: self._wrap(
                    self.client.async_set_segment_color(
                        255, 165, 0, const.ALL_FIXTURE_SEGMENTS_MASK, brightness_pct=100
                    ),
                    "fixture=ORANGE@100%",
                ),
            ),
        ]

        if self.client.wifi_enabled:
            rainbow12 = [
                (255, 0, 0),
                (255, 128, 0),
                (255, 255, 0),
                (128, 255, 0),
                (0, 255, 0),
                (0, 255, 128),
                (0, 255, 255),
                (0, 128, 255),
                (0, 0, 255),
                (128, 0, 255),
                (255, 0, 255),
                (255, 0, 128),
            ]
            steps.extend(
                [
                    (
                        "RZ1",
                        "Razer UDP Stream (12-segment rainbow, no gradient)",
                        "12 rainbow colors appear around the ring/light",
                        lambda: self._send_razer(rainbow12, gradient=False),
                    ),
                    (
                        "RZ2",
                        "Razer UDP Stream (13-segment: WHITE main + BLUE ring)",
                        "Check which zone turns WHITE vs BLUE (13-seg layout)",
                        lambda: self._send_razer(
                            [(255, 255, 255)] + [(0, 0, 255)] * 12, gradient=False
                        ),
                    ),
                    (
                        "RZ3",
                        "Razer UDP Stream OFF (`0xB1 0x00`)",
                        "Exits Razer streaming mode",
                        lambda: self._wrap(
                            self.client.async_set_razer_mode(False), "razer=OFF"
                        ),
                    ),
                ]
            )

        for step_id, title, expect, fn in steps:
            cont = await self.run_step(step_id, title, expect, fn)
            if not cont:
                break

    async def test_razer_mapping(self) -> None:
        print("\n=== SECTION 4: RAZER / DREAMVIEW ZONE & GRADIENT MAPPING ===")
        print(
            "Testing how H60A6 maps `cmd: razer` (0xB1/0xB0) across the main light,\n"
            "concentric rings, and ambient backlight halo."
        )
        await self.client.async_turn(True)
        await self.client.async_set_brightness(80)
        await self.client.async_set_color_temp(2700)
        await asyncio.sleep(0.3)

        rainbow12 = [
            (255, 0, 0),
            (255, 128, 0),
            (255, 255, 0),
            (128, 255, 0),
            (0, 255, 0),
            (0, 255, 128),
            (0, 255, 255),
            (0, 128, 255),
            (0, 0, 255),
            (128, 0, 255),
            (255, 0, 255),
            (255, 0, 128),
        ]

        steps: list[tuple[str, str, str, Callable[[], Awaitable[str]]]] = [
            (
                "Z1",
                "Razer 1 color RED (gradient=0)",
                "Note EXACTLY which part turns RED (top/backlight, bottom/main, or both)",
                lambda: self._send_razer([(255, 0, 0)], gradient=False),
            ),
            (
                "Z2",
                "Razer 1 color GREEN (gradient=1)",
                "Note which part turns GREEN with gradient=1",
                lambda: self._send_razer([(0, 255, 0)], gradient=True),
            ),
            (
                "Z3",
                "Razer 2 colors [RED, BLUE] (gradient=0)",
                "Note which part is RED and which part is BLUE",
                lambda: self._send_razer([(255, 0, 0), (0, 0, 255)], gradient=False),
            ),
            (
                "Z4",
                "Razer 2 colors [RED, BLUE] (gradient=1)",
                "Note if gradient=1 spreads RED -> BLUE across the fixture",
                lambda: self._send_razer([(255, 0, 0), (0, 0, 255)], gradient=True),
            ),
            (
                "Z5",
                "Razer 5 colors [RED, GREEN, BLUE, YELLOW, PURPLE] (gradient=0)",
                "Note if 5 concentric rings or 5 segments appear",
                lambda: self._send_razer(
                    [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (180, 0, 255)],
                    gradient=False,
                ),
            ),
            (
                "Z6",
                "Razer 12-color rainbow (gradient=1)",
                "Note if rainbow spreads smoothly across top/main/ring",
                lambda: self._send_razer(rainbow12, gradient=True),
            ),
            (
                "Z7",
                "Razer 20 colors: 10x RED + 10x CYAN (gradient=0)",
                "Note if both main AND ring split RED/CYAN",
                lambda: self._send_razer(
                    [(255, 0, 0)] * 10 + [(0, 255, 255)] * 10, gradient=False
                ),
            ),
            (
                "Z8",
                "Razer 20 colors: 10x RED + 10x CYAN (gradient=1)",
                "Note any difference with gradient=1",
                lambda: self._send_razer(
                    [(255, 0, 0)] * 10 + [(0, 255, 255)] * 10, gradient=True
                ),
            ),
            (
                "Z9",
                "Razer OFF (`0xB1 0x00`)",
                "Note what happens when Razer mode turns OFF",
                lambda: self._wrap(
                    self.client.async_set_razer_mode(False), "razer=OFF"
                ),
            ),
        ]
        for step_id, title, expect, fn in steps:
            cont = await self.run_step(step_id, title, expect, fn)
            if not cont:
                break

    async def test_segments_walk(self) -> None:
        print("\n=== SECTION 3: INDIVIDUAL SEGMENT WALK (BITS 0..12 / SEGMENTS 1..13) ===")
        proto = self.args.ring_proto
        print(f"Using segment protocol: {proto}")

        await self.client.async_turn(True)
        await self.client.async_set_brightness(80)

        # First set all 13 segments to dim red as background
        bg_mask = (1 << 13) - 1
        await self._send_seg(proto, 40, 0, 0, bg_mask, 50)
        await asyncio.sleep(0.2)

        for seg in range(1, 14):
            mask = 1 << (seg - 1)
            label = (
                "Main Panel (bit 12, mask=0x1000)"
                if seg == 13
                else f"Ring Segment {seg} (bit {seg - 1}, mask=0x{mask:04X})"
            )

            async def _light_one(m: int = mask, s: int = seg) -> str:
                # Reset previous to dim red, highlight current in bright cyan
                await self._send_seg(proto, 40, 0, 0, bg_mask, 40)
                return await self._send_seg(proto, 0, 255, 255, m, 100)

            cont = await self.run_step(
                f"S{seg}",
                f"Highlight {label}",
                f"ONLY {label} turns bright CYAN",
                _light_one,
            )
            if not cont:
                break

    async def _send_seg(
        self, proto: str, r: int, g: int, b: int, mask: int, brightness: int
    ) -> str:
        if proto == "1501":
            used = await self.client.async_set_segment_color(
                r, g, b, segment_mask=mask, brightness_pct=brightness
            )
            return f"[{used}] mask=0x{mask:04x} rgb=({r},{g},{b}) brightness={brightness}%"
        if proto == "0b":
            pkt = protocol.build_ble_segment_color_v2_packet(r, g, b, mask)
            used = await self.client.async_send_raw_packets([pkt])
            return f"[{used}] color_v2={pkt.hex()}"
        if proto == "razer":
            colors = [
                (r, g, b) if (mask & (1 << i)) else (40, 0, 0) for i in range(13)
            ]
            return await self._send_razer(colors, gradient=False)
        # "both"
        p1 = protocol.build_ble_segment_color_packet(r, g, b, mask)
        p2 = protocol.build_ble_segment_color_v2_packet(r, g, b, mask)
        br = protocol.build_ble_segment_brightness_packet(brightness, mask)
        used = await self.client.async_send_raw_packets([p1, p2, br])
        return f"[{used}] 1501={p1.hex()} | 0b={p2.hex()} | br={br.hex()}"

    async def _send_pkt(self, pkt: bytes) -> str:
        used = await self.client.async_send_raw_packets([pkt])
        return f"[{used}] ptReal/BLE hex={pkt.hex()}"

    async def _send_razer(
        self, colors: list[tuple[int, int, int]], gradient: bool
    ) -> str:
        await self.client.async_set_razer_mode(True)
        used = await self.client.async_send_razer_colors(colors, gradient=gradient)
        return f"[{used}] razer {len(colors)} segments (gradient={gradient})"

    async def _wrap(self, coro: Awaitable[str], desc: str) -> str:
        used = await coro
        return f"[{used}] {desc}"

    def print_summary(self) -> None:
        print("\n" + "=" * 68)
        print("VISUAL TEST SUMMARY (paste this back if anything failed):")
        print("=" * 68)
        for step_id, status, detail in self.results:
            print(f"  {step_id:<4}  {status:<7}  {detail}")
        print("=" * 68)

    async def run(self) -> int:
        try:
            if self.args.only == "all":
                sections = ["core", "ring", "segments"]
                if self.client.wifi_enabled:
                    sections.append("razer")
            else:
                sections = [self.args.only]
            for sec in sections:
                if sec == "core":
                    await self.test_core()
                elif sec == "ring":
                    await self.test_ring_protocols()
                elif sec == "segments":
                    await self.test_segments_walk()
                elif sec == "razer":
                    await self.test_razer_mapping()
        except KeyboardInterrupt:
            print("\nStopped by user.")
        finally:
            self.print_summary()
            await self.client.async_close()
            await self.hub.async_stop(force=True)
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Interactive visual test script for Govee H60A6 Ceiling Light Pro"
    )
    p.add_argument("--host", default=None, help="H60A6 IP address (e.g. 192.168.6.224)")
    p.add_argument("--ble", default=None, help="Optional BLE MAC address")
    p.add_argument(
        "--mode",
        choices=const.TRANSPORT_MODES,
        default=const.TRANSPORT_WIFI_PREFERRED,
        help="Transport mode (default: wifi_preferred)",
    )
    p.add_argument(
        "--only",
        choices=["all", "core", "ring", "segments", "razer"],
        default="all",
        help="Which test section to run (default: all)",
    )
    p.add_argument(
        "--ring-proto",
        choices=["both", "1501", "0b", "razer"],
        default="1501",
        help="Protocol for Section 3 segment walk (default: 1501)",
    )
    p.add_argument(
        "--listen-port",
        type=int,
        default=const.GOVEE_LISTEN_PORT,
        help="Local UDP port for devStatus replies (default: 4002)",
    )
    p.add_argument("--timeout", type=float, default=3.0, help="Status timeout (s)")
    p.add_argument(
        "--auto",
        action="store_true",
        help="Run hands-free without prompting after each step",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=2.5,
        help="Seconds between steps in --auto mode (default: 2.5)",
    )
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.mode == const.TRANSPORT_BLE_ONLY and not args.ble:
        parser.error("--ble is required when --mode ble_only")
    if args.mode != const.TRANSPORT_BLE_ONLY and not args.host and not args.ble:
        parser.error("Provide --host <IP> (or --ble <MAC> with --mode ble_only)")
    return asyncio.run(VisualTestRunner(args).run())


if __name__ == "__main__":
    raise SystemExit(main())
