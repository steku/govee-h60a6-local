#!/usr/bin/env python3
"""Standalone CLI & diagnostic tool for the Govee H60A6 Ceiling Light Pro.

Works 100% locally over Wi-Fi LAN with optional Bluetooth LE fallback,
without needing Home Assistant installed.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
from typing import Any

COMPONENT_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "govee_h60a6_local"
)


def _load_core_modules() -> tuple[Any, Any, Any, Any]:
    """Load const, protocol, scenes, and transport without triggering Home Assistant imports."""
    pkg_name = "_govee_h60a6_core"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
        sys.modules[pkg_name] = pkg

    loaded = []
    for mod_short in ("const", "protocol", "scenes", "transport"):
        full_name = f"{pkg_name}.{mod_short}"
        if full_name in sys.modules:
            loaded.append(sys.modules[full_name])
            continue
        spec = importlib.util.spec_from_file_location(
            full_name, COMPONENT_DIR / f"{mod_short}.py"
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load {mod_short}.py from {COMPONENT_DIR}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded[0], loaded[1], loaded[2], loaded[3]


const, protocol, scenes_mod, transport = _load_core_modules()


def _parse_rgb(rgb_str: str) -> tuple[int, int, int]:
    cleaned = rgb_str.strip().lstrip("#")
    if len(cleaned) == 6 and all(c in "0123456789abcdefABCDEF" for c in cleaned):
        return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))
    parts = [int(p.strip()) for p in rgb_str.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("Color must be #RRGGBB or R,G,B")
    return (parts[0], parts[1], parts[2])


async def _run(args: argparse.Namespace) -> int:
    if args.command == "scenes":
        loaded = scenes_mod.load_h60a6_scenes()
        for key, sc in loaded.items():
            param_tag = "param" if sc.scene_param else "code-only"
            print(f"  {sc.scene_code:5d}  [{sc.category}]  {sc.name} ({param_tag})")
        print(f"Total: {len(loaded)} scenes")
        return 0

    if args.command == "ble-scan":
        try:
            from bleak import BleakScanner
        except ImportError:
            print("bleak is not installed. Run: pip install bleak", file=sys.stderr)
            return 1
        print(f"Scanning for nearby Govee BLE devices for {args.timeout}s...")
        devices = await BleakScanner.discover(timeout=args.timeout)
        found = 0
        for dev in devices:
            name = dev.name or ""
            if args.all or any(k in name.lower() for k in ("govee", "ihoment", "h60a6")):
                print(f"  {dev.address}  {name or '(unnamed)'}")
                found += 1
        if not found:
            print("No matching BLE devices found (pass --all to list every BLE device).")
        return 0

    hub = transport.GoveeLanHub(listen_port=args.listen_port)
    ble = transport.GoveeBleTransport(args.ble) if args.ble else None

    if args.command != "discover":
        if args.mode == const.TRANSPORT_BLE_ONLY and not args.ble:
            print("Error: --ble MAC address is required when --mode ble_only", file=sys.stderr)
            return 2
        if args.mode in (const.TRANSPORT_WIFI_PREFERRED, const.TRANSPORT_WIFI_ONLY) and not args.host and not args.ble:
            print("Error: specify --host <IP> (or --ble <MAC> with --mode ble_only)", file=sys.stderr)
            return 2

    client = transport.GoveeH60A6Client(
        host=args.host if args.mode != const.TRANSPORT_BLE_ONLY else None,
        hub=hub if args.mode != const.TRANSPORT_BLE_ONLY else None,
        ble_transport=ble,
        transport_mode=args.mode,
        lan_timeout=args.timeout,
    )

    try:
        if args.command == "discover":
            await hub.async_start()
            devices = await hub.async_discover(timeout=args.timeout, unicast_host=args.host)
            if not devices:
                print(
                    "No Govee LAN devices responded.\n"
                    "Ensure 'LAN Control' is enabled in the Govee Home app and UDP ports 4001-4003 are open."
                )
                return 1
            for dev in devices:
                print(
                    f"Found {dev.sku or 'Govee'} at {dev.ip} | id={dev.device_id} | "
                    f"wifi={dev.wifi_version_soft} | ble={dev.ble_version_soft}"
                )
            return 0

        if args.command == "status":
            status = await client.async_get_status()
            if status is None:
                print(f"Status unavailable over current transport ({client.active_transport}).")
                return 0
            mode = (
                f"Kelvin ({status.color_temp_kelvin}K)"
                if status.color_temp_kelvin > 0
                else f"RGB {status.rgb}"
            )
            print(
                f"H60A6 @ {args.host}: power={'ON' if status.is_on else 'OFF'} | "
                f"brightness={status.brightness}% | mode={mode}"
            )
            return 0

        if args.command == "on":
            used = await client.async_turn(True)
            print(f"Power ON sent via {used}")
        elif args.command == "off":
            used = await client.async_turn(False)
            print(f"Power OFF sent via {used}")
        elif args.command == "brightness":
            used = await client.async_set_brightness(args.percent)
            print(f"Brightness {args.percent}% sent via {used}")
        elif args.command == "color":
            r, g, b = args.rgb
            used = await client.async_set_rgb(r, g, b)
            print(f"RGB ({r}, {g}, {b}) sent via {used}")
        elif args.command == "kelvin":
            used = await client.async_set_color_temp(args.kelvin)
            print(f"Color temperature {args.kelvin}K sent via {used}")
        elif args.command == "segment":
            r, g, b = args.rgb
            if args.segments == "all":
                segs = list(range(1, const.TOTAL_SEGMENT_COUNT + 1))
            elif args.segments == "ring":
                segs = list(range(1, const.RING_SEGMENT_COUNT + 1))
            elif args.segments == "center":
                segs = [const.MAIN_CENTER_SEGMENT]
            else:
                segs = [int(x) for x in args.segments.split(",")]
            mask = protocol.segments_to_bitmask(segs)
            used = await client.async_set_segment_color(
                r, g, b, segment_mask=mask, brightness_pct=args.brightness
            )
            print(f"Segments {segs} (mask=0x{mask:04x}) -> RGB({r},{g},{b}) sent via {used}")
        elif args.command == "scene":
            raw_target = str(args.scene_id).strip()
            try:
                scene_code = int(raw_target, 0)
                scene_param = ""
                label = f"#{scene_code}"
            except ValueError:
                loaded = scenes_mod.load_h60a6_scenes()
                needle = raw_target.lower()
                matched = next(
                    (
                        sc
                        for k, sc in loaded.items()
                        if sc.name.lower() == needle or k.lower() == needle
                    ),
                    None,
                )
                if matched is None:
                    matched = next(
                        (
                            sc
                            for k, sc in loaded.items()
                            if needle in sc.name.lower() or needle in k.lower()
                        ),
                        None,
                    )
                if matched is None:
                    print(f"Unknown scene {raw_target!r}. Run 'scenes' to list available scenes.", file=sys.stderr)
                    return 2
                scene_code = matched.scene_code
                scene_param = matched.scene_param
                label = f"{matched.name!r} (#{scene_code})"
            used = await client.async_set_scene(scene_code, scene_param)
            print(f"Scene {label} sent via {used}")
        elif args.command == "music":
            used = await client.async_set_music_mode(args.mode_id, args.sensitivity)
            print(f"Music mode #{args.mode_id} (sens={args.sensitivity}) sent via {used}")
        elif args.command == "raw":
            packets = [protocol.parse_hex_to_ble_packet(h) for h in args.hex]
            used = await client.async_send_raw_packets(packets)
            print(f"Sent {len(packets)} raw packet(s) via {used}: {[p.hex() for p in packets]}")
        elif args.command == "razer":
            colors = [_parse_rgb(c) for c in args.colors]
            await client.async_set_razer_mode(True)
            used = await client.async_send_razer_colors(colors, gradient=not args.no_gradient)
            print(f"Streamed {len(colors)} Razer segment colors via {used}")
        elif args.command == "probe-ring":
            probes = [
                (
                    "1) 33 05 15 01 (bytes 12-13 LE, mask=0x0FFF -> RED)",
                    [protocol.build_ble_segment_color_packet(255, 0, 0, 0x0FFF)],
                ),
                (
                    "2) 33 05 0b (bytes 8-9 LE, mask=0x0FFF -> GREEN)",
                    [protocol.build_ble_segment_color_v2_packet(0, 255, 0, 0x0FFF)],
                ),
                (
                    "3) 33 05 15 01 (bytes 12-13 LE, shifted mask=0x1FFE -> BLUE)",
                    [protocol.build_ble_segment_color_packet(0, 0, 255, 0x1FFE)],
                ),
                (
                    "4) 33 05 0b (bytes 8-9 LE, shifted mask=0x1FFE -> MAGENTA)",
                    [protocol.build_ble_segment_color_v2_packet(255, 0, 255, 0x1FFE)],
                ),
                (
                    "5) 33 05 15 01 (bytes 12-13 LE, all bits 0xFFFF -> CYAN)",
                    [protocol.build_ble_segment_color_packet(0, 255, 255, 0xFFFF)],
                ),
            ]
            print("Turning light ON and stepping through ambient ring packet formats...")
            await client.async_turn(True)
            await asyncio.sleep(0.5)
            for label, pkts in probes:
                used = await client.async_send_raw_packets(pkts)
                print(f"  [{used}] {label} | hex={pkts[0].hex()}")
                await asyncio.sleep(args.delay)
            if client.wifi_enabled:
                print("  [wifi] 6) Razer UDP 12-segment stream -> YELLOW")
                await client.async_set_razer_mode(True)
                await client.async_send_razer_colors([(255, 220, 0)] * 12, gradient=False)
                await asyncio.sleep(args.delay)
                await client.async_set_razer_mode(False)
            print("Probe complete. Note which step changed the ambient ring.")
        elif args.command == "probe-scene":
            import base64
            import logging

            logging.getLogger("_govee_h60a6_core.transport").setLevel(logging.DEBUG)
            if not logging.getLogger().handlers:
                logging.basicConfig(level=logging.INFO, format="  [log] %(message)s")

            forest_b64 = "UCwAAgEA/wAUEQAFZAYBBP//AAD/AJn/AP/DABEABU0KAQT//wD/Ov//hAAA+f8="
            raw_forest = base64.b64decode(forest_b64)
            chunk0_15, rem = raw_forest[:15], raw_forest[15:]
            tail_17 = [rem[i : i + 17] for i in range(0, len(rem), 17)]
            n_lines = 1 + len(tail_17)

            def _a3_stream(header_b2: int, header_b3: int) -> list[bytes]:
                frames = [
                    protocol.build_ble_packet([0xA3, 0x00, header_b2, header_b3, *chunk0_15])
                ]
                for idx, chunk in enumerate(tail_17, start=1):
                    line_idx = 0xFF if idx == n_lines - 1 else idx
                    frames.append(protocol.build_ble_packet([0xA3, line_idx, *chunk]))
                return frames

            # Raw 17-byte chunks without 0x01 <lines> wrapper
            raw_chunks_17 = [raw_forest[i : i + 17] for i in range(0, len(raw_forest), 17)]
            raw_a3_only = [
                protocol.build_ble_packet(
                    [0xA3, 0xFF if idx == len(raw_chunks_17) - 1 else idx, *chunk]
                )
                for idx, chunk in enumerate(raw_chunks_17)
            ]

            probes: list[tuple[str, list[bytes]]] = [
                (
                    "1) No-param Illumination(25): 33 05 04 19 00 (LE 2-byte)",
                    [protocol.build_ble_packet([0x33, 0x05, 0x04, 0x19, 0x00])],
                ),
                (
                    "2) No-param Illumination(25): 33 05 04 00 19 (BE 2-byte)",
                    [protocol.build_ble_packet([0x33, 0x05, 0x04, 0x00, 0x19])],
                ),
                (
                    "3) No-param Illumination(25): 33 05 04 19 00 00 64 (LE + flag=0 + speed=100)",
                    [protocol.build_ble_packet([0x33, 0x05, 0x04, 0x19, 0x00, 0x00, 0x64])],
                ),
                (
                    "4) No-param Illumination(25): 33 05 15 04 19 00 (sub-opcode 15 04)",
                    [protocol.build_ble_packet([0x33, 0x05, 0x15, 0x04, 0x19, 0x00])],
                ),
                (
                    "5) Forest(19073) A3 ONLY (no 33 05 04): A3 00 01 <lines> 50 2c ...",
                    _a3_stream(0x01, n_lines),
                ),
                (
                    "6) Forest(19073) A3 ONLY (no 33 05 04): A3 00 02 <lines> 50 2c ...",
                    _a3_stream(0x02, n_lines),
                ),
                (
                    "7) Forest(19073) A3 ONLY raw (no wrapper): A3 00 50 2c ...",
                    raw_a3_only,
                ),
                (
                    "8) Forest(19073) A3 00 01 <lines> + 33 05 04 81 4A (LE)",
                    [*_a3_stream(0x01, n_lines), protocol.build_ble_packet([0x33, 0x05, 0x04, 0x81, 0x4A])],
                ),
                (
                    "9) Forest(19073) A3 00 01 <lines> + 33 05 04 4A 81 (BE)",
                    [*_a3_stream(0x01, n_lines), protocol.build_ble_packet([0x33, 0x05, 0x04, 0x4A, 0x81])],
                ),
            ]
            print("Setting warm white baseline, then stepping through 9 scene formats...")
            await client.async_turn(True)
            await client.async_set_color_temp(3000)
            await asyncio.sleep(0.8)
            for label, pkts in probes:
                used = await client.async_send_raw_packets(pkts)
                print(f"\n  [{used}] {label}")
                print(f"    first={pkts[0].hex()}" + (f" last={pkts[-1].hex()}" if len(pkts) > 1 else ""))
                await asyncio.sleep(args.delay)
                # Re-restore warm white briefly so a working step is unmistakable if prior step turned off
                await client.async_turn(True)
                await client.async_set_color_temp(3000)
                await asyncio.sleep(0.5)
            print("\nProbe complete. Report which step number(s) activated a scene vs turned off.")
        return 0
    finally:
        await client.async_close()
        await hub.async_stop(force=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local Wi-Fi LAN / BLE CLI for Govee H60A6 Ceiling Light Pro"
    )
    parser.add_argument(
        "--host",
        default=None,
        help="H60A6 local IP address (required for Wi-Fi commands)",
    )
    parser.add_argument(
        "--ble",
        default=None,
        help="Optional Bluetooth MAC address (e.g. AA:BB:CC:DD:EE:FF) for fallback",
    )
    parser.add_argument(
        "--mode",
        choices=const.TRANSPORT_MODES,
        default=const.TRANSPORT_WIFI_PREFERRED,
        help="Transport preference (default: wifi_preferred)",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=const.GOVEE_LISTEN_PORT,
        help="Local UDP port for Govee replies (default: 4002)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Timeout in seconds for discovery/status (default: 3.0)",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="Scan local network for Govee LAN devices")
    sub.add_parser("status", help="Query live devStatus over LAN")
    sub.add_parser("on", help="Turn light ON")
    sub.add_parser("off", help="Turn light OFF")

    p_b = sub.add_parser("brightness", help="Set brightness (1-100%%)")
    p_b.add_argument("percent", type=int)

    p_c = sub.add_parser("color", help="Set solid RGB color (#RRGGBB or R,G,B)")
    p_c.add_argument("rgb", type=_parse_rgb)

    p_k = sub.add_parser("kelvin", help="Set white color temperature (2200-6500K)")
    p_k.add_argument("kelvin", type=int)

    p_seg = sub.add_parser("segment", help="Set segment color (1..12 = ring, 13 = center) via ptReal/BLE")
    p_seg.add_argument("segments", help="Comma-separated 1..13, 'ring' (1..12), 'center' (13), or 'all' (1..13)")
    p_seg.add_argument("rgb", type=_parse_rgb)
    p_seg.add_argument("--brightness", type=int, default=None)

    sub.add_parser("scenes", help="List available H60A6 scenes")

    p_sc = sub.add_parser("scene", help="Activate hardware scene by ID or name via ptReal/BLE")
    p_sc.add_argument("scene_id", help="Numeric scene ID or scene name")

    p_mu = sub.add_parser("music", help="Activate music mode ID via ptReal/BLE")
    p_mu.add_argument("mode_id", type=int)
    p_mu.add_argument("--sensitivity", type=int, default=50)

    p_raw = sub.add_parser("raw", help="Send raw hex ptReal/BLE frames (auto-checksummed)")
    p_raw.add_argument("hex", nargs="+", help='Hex strings e.g. "33 05 04 04"')

    p_rz = sub.add_parser("razer", help="Stream segment RGB colors via Razer LAN UDP")
    p_rz.add_argument("colors", nargs="+", help="Colors as #RRGGBB or R,G,B")
    p_rz.add_argument("--no-gradient", action="store_true")

    p_ble = sub.add_parser("ble-scan", help="Scan nearby BLE peripherals for Govee MAC")
    p_ble.add_argument("--all", action="store_true", help="Show all BLE devices")

    p_pr = sub.add_parser(
        "probe-ring",
        help="Step through candidate H60A6 ambient ring packet formats to verify firmware support",
    )
    p_pr.add_argument("--delay", type=float, default=2.5, help="Seconds between steps (default: 2.5)")

    p_ps = sub.add_parser(
        "probe-scene",
        help="Step through 9 candidate H60A6 BLE scene packet encodings and log BLE notifies",
    )
    p_ps.add_argument("--delay", type=float, default=3.0, help="Seconds between steps (default: 3.0)")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
