"""Local Wi-Fi LAN (UDP) and Bluetooth LE transports for Govee H60A6.

Design principles:
- Wi-Fi LAN is preferred: lower latency, full `devStatus` feedback, native Kelvin
  support (`colorwc`), `ptReal` passthrough, and Razer high-speed streaming.
- Shared UDP listener on port 4002 so multiple Govee devices coexist in one HA instance.
- Built-in command pacing (`command_delay_ms`) and graceful timeout handling to avoid
  triggering firmware lockups reported on H60A6 firmware >= 1.00.48.
- Optional Bluetooth LE fallback using Home Assistant's `bluetooth` stack and
  `bleak-retry-connector` (or direct `BleakClient` when run via standalone CLI).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import errno
import logging
import socket
import time
from typing import Any

from .const import (
    ALL_RING_SEGMENTS_MASK,
    BLE_HANDSHAKE_PSK,
    BLE_NOTIFY_CHAR_UUID,
    BLE_SERVICE_UUID,
    BLE_WRITE_CHAR_UUID,
    DEFAULT_BLE_IDLE_TIMEOUT,
    DEFAULT_COMMAND_DELAY_MS,
    DEFAULT_LAN_TIMEOUT,
    GOVEE_COMMAND_PORT,
    GOVEE_DISCOVERY_MULTICAST,
    GOVEE_DISCOVERY_PORT,
    GOVEE_LISTEN_PORT,
    TRANSPORT_BLE_ONLY,
    TRANSPORT_WIFI_ONLY,
    TRANSPORT_WIFI_PREFERRED,
)
from .protocol import (
    DiscoveredDevice,
    GoveeBleStatus,
    GoveeDeviceStatus,
    build_ble_brightness_packet,
    build_ble_color_temp_packet,
    build_ble_handshake_packet,
    build_ble_keepalive_packet,
    build_ble_multipacket_scene_packets,
    build_ble_music_mode_packet,
    build_ble_power_packet,
    build_ble_rgb_packet,
    build_ble_scene_packet,
    build_ble_segment_brightness_packet,
    build_ble_segment_color_packet,
    build_ble_segment_color_temp_packet,
    build_ble_status_query_packet,
    build_brightness_payload,
    build_color_payload,
    build_color_temp_payload,
    build_ptreal_payload,
    build_razer_color_frame,
    build_razer_payload,
    build_razer_toggle_frame,
    build_scan_payload,
    build_status_payload,
    build_turn_payload,
    govee_ble_decrypt,
    govee_ble_encrypt,
    parse_ble_status_frames,
    parse_lan_response,
    parse_scan_response,
    parse_status_response,
)

_LOGGER = logging.getLogger(__name__)


class GoveeTransportError(Exception):
    """Raised when both configured transports fail to deliver a command."""


# ---------------------------------------------------------------------------
# Shared Asyncio UDP Hub (Port 4002 Receiver + Port 4001/4003 Sender)
# ---------------------------------------------------------------------------


class _GoveeUdpProtocol(asyncio.DatagramProtocol):
    """Asyncio datagram protocol dispatching replies to registered waiters."""

    def __init__(self, hub: GoveeLanHub) -> None:
        self._hub = hub

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._hub._handle_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Govee UDP socket error: %s", exc)


class GoveeLanHub:
    """Manages a single shared UDP socket bound to port 4002 for LAN replies."""

    def __init__(
        self,
        listen_port: int = GOVEE_LISTEN_PORT,
        command_port: int = GOVEE_COMMAND_PORT,
        discovery_port: int = GOVEE_DISCOVERY_PORT,
        bind_host: str = "0.0.0.0",
    ) -> None:
        self.listen_port = listen_port
        self.command_port = command_port
        self.discovery_port = discovery_port
        self.bind_host = bind_host

        self._transport: asyncio.DatagramTransport | None = None
        self._status_waiters: dict[str, list[asyncio.Future[GoveeDeviceStatus]]] = {}
        self._scan_listeners: list[Callable[[DiscoveredDevice], None]] = []
        self._ref_count = 0
        self._start_lock = asyncio.Lock()
        self.receive_enabled: bool = False
        self.bound_port: int | None = None

    @property
    def is_running(self) -> bool:
        return self._transport is not None and not self._transport.is_closing()

    async def async_start(self, *, allow_ephemeral_fallback: bool = True) -> None:
        """Bind UDP listen socket if not already open.

        If `listen_port` (typically UDP 4002) is already held by another integration
        or add-on (such as Home Assistant's built-in `govee_light_local` or
        `govee2mqtt`), falls back to an ephemeral UDP port so all outbound Wi-Fi
        control commands to port 4003 still succeed without crashing setup.
        """
        async with self._start_lock:
            if self.is_running:
                self._ref_count += 1
                return

            loop = asyncio.get_running_loop()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    except OSError:
                        pass
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                except OSError:
                    pass

                try:
                    sock.bind((self.bind_host, self.listen_port))
                    self.receive_enabled = True
                except OSError as err:
                    if err.errno != errno.EADDRINUSE or not allow_ephemeral_fallback:
                        raise
                    sock.bind((self.bind_host, 0))
                    self.receive_enabled = False
                    _LOGGER.warning(
                        "Govee UDP port %s is already in use by another integration or "
                        "add-on (for example govee_light_local or govee2mqtt). Running "
                        "in Wi-Fi send-only mode on ephemeral port %s: all control "
                        "commands (power, color, Kelvin, brightness, segments, effects) "
                        "still work over Wi-Fi, while live devStatus feedback requires "
                        "freeing UDP port %s.",
                        self.listen_port,
                        sock.getsockname()[1],
                        self.listen_port,
                    )

                self.bound_port = sock.getsockname()[1]
                sock.setblocking(False)

                self._transport, _ = await loop.create_datagram_endpoint(
                    lambda: _GoveeUdpProtocol(self),
                    sock=sock,
                )
            except Exception:
                sock.close()
                raise

            self._ref_count += 1
            _LOGGER.debug(
                "Govee LAN UDP hub bound on %s:%s (receive_enabled=%s)",
                self.bind_host,
                self.bound_port,
                self.receive_enabled,
            )

    async def async_stop(self, force: bool = False) -> None:
        """Close the UDP socket when no clients remain (or immediately if forced)."""
        async with self._start_lock:
            self._ref_count = 0 if force else max(0, self._ref_count - 1)
            if self._ref_count > 0:
                return
            if self._transport is not None:
                self._transport.close()
                self._transport = None
            self.receive_enabled = False
            self.bound_port = None
            for waiters in self._status_waiters.values():
                for fut in waiters:
                    if not fut.done():
                        fut.cancel()
            self._status_waiters.clear()
            self._scan_listeners.clear()

    def send_command(self, host: str, payload: bytes) -> None:
        """Send a unicast UDP JSON command to `<host>:4003`."""
        if not self.is_running or self._transport is None:
            raise GoveeTransportError("Govee LAN UDP hub is not running")
        self._transport.sendto(payload, (host, self.command_port))

    def send_scan(self, target_host: str = GOVEE_DISCOVERY_MULTICAST) -> None:
        """Send a discovery scan packet to UDP port 4001."""
        if not self.is_running or self._transport is None:
            raise GoveeTransportError("Govee LAN UDP hub is not running")
        self._transport.sendto(build_scan_payload(), (target_host, self.discovery_port))

    async def async_query_status(
        self, host: str, timeout: float = DEFAULT_LAN_TIMEOUT
    ) -> GoveeDeviceStatus:
        """Send `devStatus` to `host` and await its UDP reply on port 4002."""
        if not self.is_running:
            await self.async_start()

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[GoveeDeviceStatus] = loop.create_future()
        self._status_waiters.setdefault(host, []).append(fut)
        try:
            self.send_command(host, build_status_payload())
            return await asyncio.wait_for(fut, timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise TimeoutError(
                f"Timed out after {timeout:.1f}s waiting for devStatus from {host}"
            ) from err
        finally:
            waiters = self._status_waiters.get(host, [])
            if fut in waiters:
                waiters.remove(fut)
            if not waiters and host in self._status_waiters:
                del self._status_waiters[host]

    async def async_discover(
        self,
        timeout: float = 3.0,
        unicast_host: str | None = None,
    ) -> list[DiscoveredDevice]:
        """Discover Govee LAN devices via multicast (and optional unicast probe)."""
        if not self.is_running:
            await self.async_start()

        found: dict[str, DiscoveredDevice] = {}

        def _on_device(dev: DiscoveredDevice) -> None:
            found[dev.ip] = dev

        self._scan_listeners.append(_on_device)
        try:
            try:
                self.send_scan(GOVEE_DISCOVERY_MULTICAST)
            except OSError as err:
                _LOGGER.debug("Multicast scan send failed: %s", err)
            try:
                self.send_scan("255.255.255.255")
            except OSError as err:
                _LOGGER.debug("Broadcast scan send failed: %s", err)
            if unicast_host:
                try:
                    self.send_scan(unicast_host)
                except OSError as err:
                    _LOGGER.debug("Unicast scan send to %s failed: %s", unicast_host, err)
            await asyncio.sleep(timeout)
        finally:
            if _on_device in self._scan_listeners:
                self._scan_listeners.remove(_on_device)

        return list(found.values())

    def _handle_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        sender_ip = addr[0]
        parsed = parse_lan_response(data)
        if parsed is None:
            _LOGGER.debug("Ignoring non-Govee UDP packet from %s", sender_ip)
            return

        cmd, payload = parsed
        if cmd == "devStatus":
            status = parse_status_response(payload)
            if status is None:
                return
            waiters = self._status_waiters.get(sender_ip)
            if waiters:
                for fut in list(waiters):
                    if not fut.done():
                        fut.set_result(status)
        elif cmd == "scan":
            dev = parse_scan_response(payload, sender_ip)
            if dev is None:
                return
            for listener in list(self._scan_listeners):
                try:
                    listener(dev)
                except Exception:  # pragma: no cover - defensive
                    _LOGGER.exception("Error in Govee scan listener")


# ---------------------------------------------------------------------------
# Bluetooth LE Transport (HA Bluetooth stack + bleak-retry-connector)
# ---------------------------------------------------------------------------


class GoveeBleTransport:
    """Async BLE GATT transport for Govee 20-byte packets.

    Uses Home Assistant's central `bluetooth` manager when `hass` is provided,
    and falls back to direct `bleak.BleakClient` in standalone CLI usage.
    Automatically disconnects after an idle period to free BLE proxy slots.
    """

    def __init__(
        self,
        address: str,
        hass: Any | None = None,
        idle_timeout: float = DEFAULT_BLE_IDLE_TIMEOUT,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.address = address.upper()
        self._hass = hass
        self._idle_timeout = idle_timeout
        self._client_factory = client_factory

        self._client: Any | None = None
        self._lock = asyncio.Lock()
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._session_key: bytes | None = None
        self._handshake_future: asyncio.Future[bytes] | None = None
        self._handshake_expected_step: int | None = None
        self._ac_frames: list[bytes] = []
        self._ac_done: asyncio.Future[list[bytes]] | None = None

    @property
    def is_connected(self) -> bool:
        return bool(self._client is not None and getattr(self._client, "is_connected", False))

    async def _ensure_connected(self) -> Any:
        if self.is_connected:
            return self._client

        if self._client_factory is not None:
            client = self._client_factory()
            if hasattr(client, "connect"):
                await client.connect()
            if hasattr(client, "start_notify"):
                try:
                    await client.start_notify(BLE_NOTIFY_CHAR_UUID, self._on_notify)
                except Exception as err:
                    _LOGGER.debug("Mock BLE start_notify skipped for %s: %s", self.address, err)
            self._client = client
            return client

        if self._hass is not None:
            from bleak_retry_connector import (
                BleakClientWithServiceCache,
                establish_connection,
            )
            from homeassistant.components import bluetooth

            ble_device = bluetooth.async_ble_device_from_address(
                self._hass, self.address, connectable=True
            )
            if ble_device is None:
                raise GoveeTransportError(
                    f"Bluetooth device {self.address} not seen by Home Assistant Bluetooth"
                )
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                ble_device.name or self.address,
                disconnected_callback=self._on_disconnected,
            )
        else:
            from bleak import BleakClient

            client = BleakClient(
                self.address,
                disconnected_callback=self._on_disconnected,
                services=[BLE_SERVICE_UUID],
            )
            await client.connect()
            self._client = client

        try:
            await self._client.start_notify(BLE_NOTIFY_CHAR_UUID, self._on_notify)
            await self._async_handshake(self._client)
        except Exception as err:
            _LOGGER.debug("BLE notify/handshake skipped for %s: %s", self.address, err)

        return self._client

    def _on_disconnected(self, _client: Any) -> None:
        _LOGGER.debug("Govee BLE disconnected from %s", self.address)
        self._client = None
        self._session_key = None
        if self._handshake_future is not None and not self._handshake_future.done():
            self._handshake_future.cancel()
        self._handshake_future = None
        self._handshake_expected_step = None
        if self._ac_done is not None and not self._ac_done.done():
            self._ac_done.cancel()
        self._ac_done = None
        self._ac_frames = []

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        raw = bytes(data)
        if (
            self._handshake_future is not None
            and not self._handshake_future.done()
            and len(raw) >= 16
        ):
            try:
                dec = govee_ble_decrypt(raw, BLE_HANDSHAKE_PSK)
            except Exception:
                dec = b""
            if len(dec) >= 2 and dec[0] == 0xE7 and dec[1] == self._handshake_expected_step:
                self._handshake_future.set_result(dec)
                return

        dec: bytes | None = None
        if self._session_key is not None and raw:
            try:
                dec = govee_ble_decrypt(raw, self._session_key)
                _LOGGER.debug("Govee BLE notify (%s): %s", self.address, dec.hex())
            except Exception:
                dec = None
        if dec is None:
            dec = raw
            _LOGGER.debug("Govee BLE notify (%s): %s", self.address, raw.hex())

        if (
            self._ac_done is not None
            and not self._ac_done.done()
            and len(dec) >= 2
            and dec[0] == 0xAC
        ):
            self._ac_frames.append(dec)
            if dec[1] == 0xFF:
                self._ac_done.set_result(list(self._ac_frames))

    async def _async_handshake(self, client: Any) -> None:
        """Perform the MakingLifeSmarte E701/E702 session key handshake if supported."""
        loop = asyncio.get_running_loop()
        self._session_key = None
        try:
            fut1: asyncio.Future[bytes] = loop.create_future()
            self._handshake_expected_step = 0x01
            self._handshake_future = fut1
            tx1 = govee_ble_encrypt(build_ble_handshake_packet(0x01), BLE_HANDSHAKE_PSK)
            await client.write_gatt_char(BLE_WRITE_CHAR_UUID, tx1, response=False)
            try:
                dec1 = await asyncio.wait_for(fut1, timeout=2.0)
            except (TimeoutError, asyncio.TimeoutError):
                _LOGGER.debug(
                    "Govee BLE %s did not respond to E701 handshake; using plaintext",
                    self.address,
                )
                return

            session_key = dec1[2:18]
            if len(session_key) != 16:
                return

            fut2: asyncio.Future[bytes] = loop.create_future()
            self._handshake_expected_step = 0x02
            self._handshake_future = fut2
            tx2 = govee_ble_encrypt(build_ble_handshake_packet(0x02), BLE_HANDSHAKE_PSK)
            await client.write_gatt_char(BLE_WRITE_CHAR_UUID, tx2, response=False)
            try:
                await asyncio.wait_for(fut2, timeout=1.0)
            except (TimeoutError, asyncio.TimeoutError):
                pass

            self._session_key = session_key
            _LOGGER.debug("Govee BLE %s negotiated encrypted session", self.address)
        finally:
            self._handshake_future = None
            self._handshake_expected_step = None

    def _schedule_idle_disconnect(self) -> None:
        if self._disconnect_timer is not None:
            self._disconnect_timer.cancel()
        if self._idle_timeout <= 0:
            return
        loop = asyncio.get_running_loop()
        self._disconnect_timer = loop.call_later(
            self._idle_timeout,
            lambda: asyncio.create_task(self.async_disconnect()),
        )

    async def async_send_packets(self, packets: list[bytes]) -> None:
        """Write one or more 20-byte packets to the Govee BLE write characteristic."""
        async with self._lock:
            try:
                client = await self._ensure_connected()
                for idx, packet in enumerate(packets):
                    wire_packet = (
                        govee_ble_encrypt(packet, self._session_key)
                        if self._session_key is not None
                        else packet
                    )
                    await client.write_gatt_char(
                        BLE_WRITE_CHAR_UUID, wire_packet, response=False
                    )
                    if idx + 1 < len(packets):
                        next_pkt = packets[idx + 1]
                        if packet[0] in (0xA3, 0xA4) and next_pkt[0] == 0x33:
                            await asyncio.sleep(0.22)
                        else:
                            await asyncio.sleep(0.03)
                self._schedule_idle_disconnect()
            except Exception as err:
                await self._disconnect_unlocked()
                raise GoveeTransportError(
                    f"BLE write to {self.address} failed: {err}"
                ) from err

    async def async_keepalive(self) -> None:
        """Send BLE keepalive heartbeat packet."""
        await self.async_send_packets([build_ble_keepalive_packet()])

    async def async_query_status(self, timeout: float = 3.0) -> list[bytes]:
        """Query full live H60A6 status over BLE (`ac 03 03 41 30 a5`)."""
        async with self._lock:
            try:
                client = await self._ensure_connected()
                loop = asyncio.get_running_loop()
                self._ac_frames = []
                fut: asyncio.Future[list[bytes]] = loop.create_future()
                self._ac_done = fut
                pkt = build_ble_status_query_packet()
                wire_packet = (
                    govee_ble_encrypt(pkt, self._session_key)
                    if self._session_key is not None
                    else pkt
                )
                await client.write_gatt_char(
                    BLE_WRITE_CHAR_UUID, wire_packet, response=False
                )
                frames = await asyncio.wait_for(fut, timeout=timeout)
                self._schedule_idle_disconnect()
                return frames
            except Exception as err:
                await self._disconnect_unlocked()
                raise GoveeTransportError(
                    f"BLE status query to {self.address} failed: {err}"
                ) from err
            finally:
                self._ac_done = None
                self._ac_frames = []

    async def async_disconnect(self) -> None:
        """Disconnect cleanly from the BLE peripheral."""
        async with self._lock:
            await self._disconnect_unlocked()

    async def _disconnect_unlocked(self) -> None:
        if self._disconnect_timer is not None:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None
        client = self._client
        self._client = None
        self._session_key = None
        if self._handshake_future is not None and not self._handshake_future.done():
            self._handshake_future.cancel()
        self._handshake_future = None
        self._handshake_expected_step = None
        if self._ac_done is not None and not self._ac_done.done():
            self._ac_done.cancel()
        self._ac_done = None
        self._ac_frames = []
        if client is not None and getattr(client, "is_connected", False):
            try:
                await client.disconnect()
            except Exception as err:
                _LOGGER.debug("Error disconnecting BLE %s: %s", self.address, err)


# ---------------------------------------------------------------------------
# Hybrid H60A6 Client (Wi-Fi LAN Preferred + Optional BLE Fallback)
# ---------------------------------------------------------------------------


class GoveeH60A6Client:
    """High-level local controller for a Govee H60A6 ceiling light.

    Supports Wi-Fi preferred (automatic BLE fallback), Wi-Fi only, or BLE only.
    Enforces gentle command pacing to protect H60A6 Wi-Fi firmware from stalls.
    """

    def __init__(
        self,
        host: str | None,
        hub: GoveeLanHub | None = None,
        ble_transport: GoveeBleTransport | None = None,
        transport_mode: str = TRANSPORT_WIFI_PREFERRED,
        command_delay_ms: int = DEFAULT_COMMAND_DELAY_MS,
        lan_timeout: float = DEFAULT_LAN_TIMEOUT,
    ) -> None:
        self.host = host.strip() if host else None
        self.hub = hub
        self.ble = ble_transport
        self.transport_mode = transport_mode
        self.command_delay_s = max(0.0, command_delay_ms / 1000.0)
        self.lan_timeout = lan_timeout

        self._cmd_lock = asyncio.Lock()
        self._last_wifi_cmd_ts = 0.0
        self._wifi_healthy = bool(self.host)
        self._consecutive_wifi_failures = 0
        self.active_transport: str = "offline"

    @property
    def wifi_enabled(self) -> bool:
        return (
            self.transport_mode in (TRANSPORT_WIFI_PREFERRED, TRANSPORT_WIFI_ONLY)
            and bool(self.host)
            and self.hub is not None
        )

    @property
    def ble_enabled(self) -> bool:
        return (
            self.transport_mode in (TRANSPORT_WIFI_PREFERRED, TRANSPORT_BLE_ONLY)
            and self.ble is not None
        )

    async def _pace_wifi(self) -> None:
        """Ensure minimum gap between Wi-Fi UDP packets to prevent H60A6 freezes."""
        if self.command_delay_s <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_wifi_cmd_ts
        if elapsed < self.command_delay_s:
            await asyncio.sleep(self.command_delay_s - elapsed)
        self._last_wifi_cmd_ts = time.monotonic()

    async def _send_wifi_payload(self, payload: bytes) -> None:
        if not self.wifi_enabled or self.hub is None or not self.host:
            raise GoveeTransportError("Wi-Fi LAN transport is not configured")
        if not self.hub.is_running:
            await self.hub.async_start()
        await self._pace_wifi()
        self.hub.send_command(self.host, payload)
        self.active_transport = "wifi" if self.hub.receive_enabled else "wifi_send_only"

    async def _dispatch(
        self,
        wifi_payload: bytes | None,
        ble_packets: list[bytes] | None,
    ) -> str:
        """Dispatch a command preferring Wi-Fi LAN and falling back to BLE."""
        async with self._cmd_lock:
            errors: list[str] = []

            # If Wi-Fi is preferred and has not been marked down (or BLE isn't available),
            # try Wi-Fi first.
            if self.wifi_enabled and wifi_payload is not None:
                if self._wifi_healthy or not self.ble_enabled:
                    try:
                        await self._send_wifi_payload(wifi_payload)
                        return "wifi"
                    except Exception as err:
                        self._consecutive_wifi_failures += 1
                        if self._consecutive_wifi_failures >= 2:
                            self._wifi_healthy = False
                        errors.append(f"Wi-Fi: {err}")
                        _LOGGER.warning(
                            "Govee H60A6 Wi-Fi send to %s failed: %s", self.host, err
                        )

            if self.ble_enabled and ble_packets and self.ble is not None:
                try:
                    await self.ble.async_send_packets(ble_packets)
                    self.active_transport = "ble"
                    return "ble"
                except Exception as err:
                    errors.append(f"BLE: {err}")
                    _LOGGER.warning(
                        "Govee H60A6 BLE send to %s failed: %s", self.ble.address, err
                    )

            # Last resort: if Wi-Fi was skipped because it was previously unhealthy,
            # try it once before giving up.
            if self.wifi_enabled and wifi_payload is not None and not self._wifi_healthy:
                try:
                    await self._send_wifi_payload(wifi_payload)
                    self._wifi_healthy = True
                    self._consecutive_wifi_failures = 0
                    return "wifi"
                except Exception as err:
                    errors.append(f"Wi-Fi retry: {err}")

            self.active_transport = "offline"
            raise GoveeTransportError(
                "; ".join(errors) if errors else "No transport available for command"
            )

    async def async_get_status(self) -> GoveeDeviceStatus | None:
        """Query live device status via Wi-Fi LAN `devStatus`.

        Returns `None` in BLE-only mode (BLE does not expose LAN `devStatus` JSON).
        Marks Wi-Fi healthy/unhealthy so subsequent commands automatically fall back
        to BLE if the light stops responding over Wi-Fi.
        """
        if not self.wifi_enabled or self.hub is None or not self.host:
            if self.ble_enabled:
                self.active_transport = "ble"
            return None

        if not self.hub.is_running:
            await self.hub.async_start()

        if not self.hub.receive_enabled:
            self._wifi_healthy = True
            self._consecutive_wifi_failures = 0
            self.active_transport = "wifi_send_only"
            return None

        try:
            status = await self.hub.async_query_status(
                self.host, timeout=self.lan_timeout
            )
            self._wifi_healthy = True
            self._consecutive_wifi_failures = 0
            self.active_transport = "wifi"
            return status
        except Exception as err:
            self._consecutive_wifi_failures += 1
            if self._consecutive_wifi_failures >= 2:
                self._wifi_healthy = False
            if self.ble_enabled:
                self.active_transport = "ble"
                _LOGGER.debug(
                    "Wi-Fi devStatus failed for %s (%s); BLE fallback ready",
                    self.host,
                    err,
                )
                return None
            self.active_transport = "offline"
            raise GoveeTransportError(
                f"Failed to query devStatus from {self.host}: {err}"
            ) from err

    async def async_get_ble_status(self, timeout: float = 3.0) -> GoveeBleStatus | None:
        """Query full H60A6 status (power, brightness, Kelvin, scene, segments) over BLE."""
        if not self.ble_enabled or self.ble is None:
            return None
        frames = await self.ble.async_query_status(timeout=timeout)
        return parse_ble_status_frames(frames)

    async def async_turn(self, is_on: bool) -> str:
        """Turn the H60A6 on or off."""
        return await self._dispatch(
            build_turn_payload(is_on),
            [build_ble_power_packet(is_on)],
        )

    async def async_set_brightness(self, brightness_pct: int) -> str:
        """Set overall brightness (1..100%)."""
        return await self._dispatch(
            build_brightness_payload(brightness_pct),
            [build_ble_brightness_packet(brightness_pct)],
        )

    async def async_set_rgb(self, r: int, g: int, b: int) -> str:
        """Set solid RGB color (0..255 per channel)."""
        return await self._dispatch(
            build_color_payload(r, g, b),
            [build_ble_rgb_packet(r, g, b)],
        )

    async def async_set_color_temp(self, kelvin: int) -> str:
        """Set white color temperature in Kelvin (2200K..6500K)."""
        return await self._dispatch(
            build_color_temp_payload(kelvin),
            [build_ble_color_temp_packet(kelvin)],
        )

    async def _dispatch_ptreal_packets(self, packets: list[bytes]) -> str:
        """Send one or more 20-byte frames via BLE when configured, else Wi-Fi `ptReal`.

        H60A6 firmware ignores `cmd: "ptReal"` over Wi-Fi LAN UDP 4003 for
        ambient ring, per-segment, and scene commands, while accepting the exact
        same 20-byte frames directly over Bluetooth LE GATT. Therefore, when BLE
        is configured, send strictly over BLE and let errors propagate so HA does
        not optimistically update segment state when BLE delivery fails.
        """
        if self.ble_enabled and self.ble is not None:
            await self.ble.async_send_packets(packets)
            return "ble"

        last_transport = "offline"
        for pkt in packets:
            last_transport = await self._dispatch(build_ptreal_payload([pkt]), [pkt])
        return last_transport

    async def async_set_segment_color(
        self,
        r: int,
        g: int,
        b: int,
        segment_mask: int = ALL_RING_SEGMENTS_MASK,
        brightness_pct: int | None = None,
    ) -> str:
        """Set RGB color (and optional brightness) on one or more H60A6 segments."""
        if brightness_pct is not None:
            scale = max(0, min(100, int(brightness_pct))) / 100.0
            r = round(r * scale)
            g = round(g * scale)
            b = round(b * scale)
        packets = [build_ble_segment_color_packet(r, g, b, segment_mask)]
        return await self._dispatch_ptreal_packets(packets)

    async def async_set_segment_color_temp(
        self,
        kelvin: int,
        segment_mask: int = ALL_RING_SEGMENTS_MASK,
        brightness_pct: int | None = None,
    ) -> str:
        """Set dedicated WW/CW Kelvin color temperature on one or more segments."""
        packets = [build_ble_segment_color_temp_packet(kelvin, segment_mask)]
        return await self._dispatch_ptreal_packets(packets)

    async def async_set_segment_brightness(
        self,
        brightness_pct: int,
        segment_mask: int = ALL_RING_SEGMENTS_MASK,
    ) -> str:
        """Set brightness (0..100%) on specific H60A6 segments."""
        if brightness_pct <= 0:
            packets = [build_ble_segment_color_packet(0, 0, 0, segment_mask)]
        else:
            packets = [build_ble_segment_brightness_packet(brightness_pct, segment_mask)]
        return await self._dispatch_ptreal_packets(packets)

    async def async_set_scene(
        self, scene_id: int, scene_param_b64: str | None = None
    ) -> str:
        """Activate a native Govee hardware scene via `ptReal` / BLE."""
        packets = build_ble_multipacket_scene_packets(scene_id, scene_param_b64)
        return await self._dispatch_ptreal_packets(packets)

    async def async_set_music_mode(self, mode_id: int, sensitivity: int = 50) -> str:
        """Activate a native Govee onboard music mode via `ptReal` / BLE."""
        return await self._dispatch_ptreal_packets(
            [build_ble_music_mode_packet(mode_id, sensitivity)]
        )

    async def async_send_raw_packets(self, packets: list[bytes]) -> str:
        """Send raw 20-byte Govee BLE frames over LAN `ptReal` or BLE."""
        return await self._dispatch_ptreal_packets(packets)

    async def async_set_razer_mode(self, enable: bool) -> str:
        """Enable or disable high-speed local Razer/DreamView streaming (Wi-Fi LAN)."""
        payload = build_razer_payload(build_razer_toggle_frame(enable))
        return await self._dispatch(payload, None)

    async def async_send_razer_colors(
        self,
        colors: list[tuple[int, int, int]],
        gradient: bool = True,
    ) -> str:
        """Stream an array of segment RGB colors via local Razer/DreamView UDP."""
        payload = build_razer_payload(build_razer_color_frame(colors, gradient=gradient))
        return await self._dispatch(payload, None)

    async def async_close(self) -> None:
        """Release BLE connection if active."""
        if self.ble is not None:
            await self.ble.async_disconnect()
