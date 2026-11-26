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


def _load_core_modules() -> tuple[Any, Any, Any]:
    """Load const, protocol, and transport without triggering Home Assistant imports."""
    pkg_name = "_govee_h60a6_core"
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
            raise RuntimeError(f"Could not load {mod_short}.py from {COMPONENT_DIR}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded[0], loaded[1], loaded[2]


const, protocol, transport = _load_core_modules()


def _parse_rgb(rgb_str: str) -> tuple[int, int, int]:
    cleaned = rgb_str.strip().lstrip("#")
    if len(cleaned) == 6 and all(c in "0123456789abcdefABCDEF" for c in cleaned):
        return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))
    parts = [int(p.strip()) for p in rgb_str.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("Color must be #RRGGBB or R,G,B")
    return (parts[0], parts[1], parts[2])


async def _run(args: argparse.Namespace) -> int:
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
            segs = (
                list(range(1, const.RING_SEGMENT_COUNT + 1))
                if args.segments == "all"
                else [int(x) for x in args.segments.split(",")]
            )
            mask = protocol.segments_to_bitmask(segs)
            used = await client.async_set_segment_color(
                r, g, b, segment_mask=mask, brightness_pct=args.brightness
            )
            print(f"Segments {segs} (mask=0x{mask:04x}) -> RGB({r},{g},{b}) sent via {used}")
        elif args.command == "scene":
            used = await client.async_set_scene(args.scene_id)
            print(f"Scene #{args.scene_id} sent via {used}")
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

    p_seg = sub.add_parser("segment", help="Set ambient ring segment color via ptReal/BLE")
    p_seg.add_argument("segments", help="Comma-separated 1..12 or 'all'")
    p_seg.add_argument("rgb", type=_parse_rgb)
    p_seg.add_argument("--brightness", type=int, default=None)

    p_sc = sub.add_parser("scene", help="Activate hardware scene ID via ptReal/BLE")
    p_sc.add_argument("scene_id", type=int)

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

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
