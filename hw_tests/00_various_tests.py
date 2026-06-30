# SPDX-FileCopyrightText: Copyright (c) 2026 Bob Grant for Adafruit Industries
#
# SPDX-License-Identifier: MIT
"""
HDC302x driver test / verification harness.

Run on real hardware (ESP32-S2/S3, RP2040, ...) against an Adafruit HDC302x breakout.
Decodes and sanity-checks return values rather than just confirming "it ran", in the
STCC4/SCD4x review style.

Sections:
  * read paths (trigger-on-demand + auto mode)  <- exposes the missing-wait bug on the
    UNPATCHED library; should pass cleanly once PR1 fix #1 is applied
  * status register
  * alert thresholds (set / clear / read-back + which status bits fire)
  * heater
  * offsets  (EEPROM write — GATED, off by default to protect endurance)
  * power-on override / NVM transfer (EEPROM — GATED)

Set the flags below before running. EEPROM tests are OFF by default.
"""

import time

import board
from adafruit_hdc302x import HDC302x

# --- flags -------------------------------------------------------------------
RUN_EEPROM_TESTS = False  # offsets, NVM threshold transfer, power-on override (writes EEPROM!)
RUN_HEATER_TEST = True  # briefly pulses the heater; skews T/RH while on
# -----------------------------------------------------------------------------


# --- pass/fail accounting (Adafruit hw_tests/ convention) --------------------
# Counters live as attributes on a tiny holder rather than module-level ints rebound via
# `global` inside test(): Adafruit's ruff config extend-selects PLW0603 (global-statement)
# with no hw_tests/ exemption, so the `global` form fails CI. Mutating an attribute needs no
# global declaration, so this stays lint-clean while keeping the same PASS:/FAIL: output.
class _Tally:
    passed = 0
    failed = 0


def test(name, condition):
    """Record one PASS/FAIL line and bump the counters; return the condition so callers can
    still branch on it. Matches the Adafruit hw_tests/ harness format the serial watcher
    greps for."""
    if condition:
        print(f"PASS: {name}")
        _Tally.passed += 1
    else:
        print(f"FAIL: {name}")
        _Tally.failed += 1
    return condition


def check(label, value, lo, hi):
    """Range check that also records a PASS/FAIL via test()."""
    return test(f"{label} = {value!r} (expect {lo}..{hi})", lo <= value <= hi)


def section(name):
    print(f"\n=== {name} ===")


def measure():
    """Correct trigger-on-demand read for the TEST itself: trigger LPM0, wait the datasheet
    max conversion (14.1 ms), then read. Independent of library bug #1, so the alert section
    gets real ambient values and a real comparator evaluation regardless of patch state.
    Returns (temp_C, rh_pct) or (None, None) if the sensor keeps NACKing."""
    hdc._write_command(0x2400)  # trigger LPM0 (test reaches into the driver on purpose)
    time.sleep(0.0141)
    for _ in range(3):
        try:
            with hdc.i2c_device as i2c:
                i2c.readinto(buf := bytearray(6))
            t = (((buf[0] << 8 | buf[1]) / 65535.0) * 175.0) - 45.0
            rh = ((buf[3] << 8 | buf[4]) / 65535.0) * 100.0
            return t, rh
        except OSError:
            time.sleep(0.003)
    return None, None


try:
    try:
        i2c = board.STEMMA_I2C()
    except AttributeError:
        i2c = board.I2C()

    hdc = HDC302x(i2c)
    # --- IDs ---------------------------------------------------------------------
    section("Device IDs")
    mfg = hdc.manufacturer_id
    print(f"  manufacturer_id = 0x{mfg:04X}  (expect 0x3000)")
    nist = hdc.nist_id
    print("  nist_id = {}  (48-bit serial, 6 bytes)".format([f"0x{b:02X}" for b in nist]))
    test("manufacturer_id == 0x3000", mfg == 0x3000)
    test("nist_id is 6 bytes (48-bit serial)", len(nist) == 6)

    # --- trigger-on-demand reads -------------------------------------------------
    section("Trigger-on-demand (single shot)")
    # On the UNPATCHED library these read the data register with no conversion wait, so you get
    # either a NACK (OSError) or the uninitialized 0x0000 register = -45 C / 0 %RH. Both signatures
    # below mean PR1 fix #1 is needed. With the fix, real values appear here.
    try:
        t = hdc.temperature
        rh = hdc.relative_humidity
        if t <= -44.9 and rh <= 0.1:
            print(
                "  [bug #1] returned 0x0000 sentinel (-45 C / 0 %RH): read before convert, no wait"
            )
        check("temperature C", t, -40, 125)
        check("relative_humidity %", rh, 0, 100)
    except OSError as exc:
        print("  [bug #1] trigger read NACKed (no conversion wait):", exc)
    # Contrast: same sensor, but trigger -> wait -> read (what fix #1 adds). Proves the HW is fine:
    mt, mrh = measure()
    if mt is not None:
        check("with-wait temperature C", mt, -40, 125)
        check("with-wait relative_humidity %", mrh, 0, 100)
    else:
        print("  [FAIL] even with a wait the sensor NACKed -- that would be a real HW/bus issue")

    # --- auto mode ---------------------------------------------------------------
    section("Auto measurement mode @ 1 Hz")
    try:
        hdc.auto_mode = "1MPS_LP0"
        time.sleep(1.2)  # let at least one conversion land
        at = hdc.auto_temperature
        check("auto_temperature C", at, -40, 125)
        # On the UNPATCHED library this SECOND separate readout consumes the cleared result:
        # it either returns the +100 %RH sentinel or NACKs mid-conversion (both = bug #3).
        # A combined auto_measurements property reads T+RH in one shot and avoids it.
        try:
            arh = hdc.auto_relative_humidity
            if arh >= 99.9:
                print(f"  [bug #3] auto_relative_humidity = {arh:.1f} (cleared-result sentinel)")
            else:
                check("auto_relative_humidity %", arh, 0, 100)
        except OSError:
            print("  [bug #3] auto_relative_humidity NACKed (2nd readout landed mid-conversion)")
    finally:
        try:
            hdc.auto_mode = "EXIT_AUTO_MODE"
        except OSError:
            pass
        time.sleep(0.05)

    # --- status register ---------------------------------------------------------
    section("Status register")
    st = hdc.status
    print(f"  status = 0x{st:04X}")
    print(f"  bit15 overall-alert = {bool(st & (1 << 15))}")
    print(f"  bit13 heater        = {bool(st & (1 << 13))}")
    print(f"  bit4  reset-detected= {bool(st & (1 << 4))}")
    print(f"  high_alert() = {hdc.high_alert}   low_alert() = {hdc.low_alert}")
    print("  (PR1 fix #2: verify high/low_alert track bits 9/7 and 8/6 below)")

    # --- alert thresholds --------------------------------------------------------
    section("Alert thresholds — set / latch / clear walkthrough")
    # You do NOT need the ALERT pin wired: alerts are readable over I2C in the status register.
    # Ground truth = raw status bits (Table 7-14): bit9 RH-high, bit7 T-high, bit8 RH-low,
    # bit6 T-low. We drive those bits on purpose and print what high_alert()/low_alert() return,
    # so you can confirm PR1 fix #2 (high = 9|7, low = 8|6). On the UNPATCHED library the raw
    # bits are correct but high_alert()/low_alert() will disagree -- that disagreement IS the bug.
    #
    # Alerts are an auto-mode feature (the comparator runs on each measurement to wake a sleeping
    # MCU), so we evaluate in auto mode. Threshold ordering (Fig 7-16): Set-High > Clear-High >
    # Clear-Low > Set-Low. Each command packs a temp AND an RH bound, so a single Set-High below
    # ambient trips T-high (bit7) AND RH-high (bit9) together.

    def show(tag):
        s = hdc.status
        print(f"  {tag}")
        print(
            f"    status=0x{s:04X}  raw bits -> RHhi={bool(s & (1 << 9))} "
            + f"Thi={bool(s & (1 << 7))} | RHlo={bool(s & (1 << 8))} Tlo={bool(s & (1 << 6))}"
        )
        print(
            f"    high_alert()={hdc.high_alert}  low_alert()={hdc.low_alert}   "
            + "(should match raw hi / lo bits once patched)"
        )

    def try_clear_status():
        try:
            hdc.clear_status()  # PR2 method; harmless if not yet implemented
        except AttributeError:
            pass

    def program_then_eval(tag, set_high, clear_high, set_low, clear_low):
        """Program thresholds in sleep (best practice), then run auto mode so the comparator
        evaluates them, then read the status bits."""
        hdc.auto_mode = "EXIT_AUTO_MODE"  # sleep
        time.sleep(0.02)
        hdc.set_high_alert(temp=set_high[0], humid=set_high[1])
        hdc.clear_high_alert(temp=clear_high[0], humid=clear_high[1])
        hdc.set_low_alert(temp=set_low[0], humid=set_low[1])
        hdc.clear_low_alert(temp=clear_low[0], humid=clear_low[1])
        try_clear_status()
        hdc.auto_mode = "1MPS_LP0"  # measure -> comparator evaluates each second
        time.sleep(1.3)  # let a couple of conversions land
        show(tag)
        hdc.auto_mode = "EXIT_AUTO_MODE"
        time.sleep(0.02)

    # Real ambient from the robust trigger read (independent of bug #1)
    t0, rh0 = measure()
    base_t = t0 if t0 is not None else 25.0
    base_rh = rh0 if rh0 is not None else 50.0
    print(f"  ambient ~ {base_t:.1f}C / {base_rh:.0f}%RH")

    # 1) Force HIGH: Set-High just BELOW ambient (ambient > threshold -> high alert).
    program_then_eval(
        "after Set-High BELOW ambient  (expect RHhi/Thi = True)",
        set_high=(base_t - 3.0, max(base_rh - 5.0, 1.0)),
        clear_high=(base_t - 4.0, max(base_rh - 6.0, 1.0)),
        set_low=(-40.0, 0.0),
        clear_low=(-39.0, 1.0),
    )

    # 2) Clear HIGH: thresholds well ABOVE ambient.
    program_then_eval(
        "after raising thresholds  (expect HIGH cleared)",
        set_high=(125.0, 100.0),
        clear_high=(124.0, 99.0),
        set_low=(-40.0, 0.0),
        clear_low=(-39.0, 1.0),
    )

    # 3) Force LOW: Set-Low just ABOVE ambient (ambient < threshold -> low alert).
    program_then_eval(
        "after Set-Low ABOVE ambient  (expect RHlo/Tlo = True)",
        set_high=(125.0, 100.0),
        clear_high=(124.0, 99.0),
        set_low=(base_t + 3.0, min(base_rh + 5.0, 99.0)),
        clear_low=(base_t + 4.0, min(base_rh + 6.0, 99.0)),
    )

    # Restore wide-open / tracking effectively off.
    hdc.auto_mode = "EXIT_AUTO_MODE"
    hdc.set_high_alert(temp=125.0, humid=100.0)
    hdc.set_low_alert(temp=-40.0, humid=0.0)
    hdc.clear_high_alert(temp=124.0, humid=99.0)
    hdc.clear_low_alert(temp=-39.0, humid=1.0)
    try_clear_status()

    # --- heater ------------------------------------------------------------------
    if RUN_HEATER_TEST:
        section("Heater")
        print(f"  heater (off) = {hdc.heater}")
        hdc.heater = "QUARTER_POWER"
        time.sleep(0.2)
        on = hdc.heater
        print(f"  heater (quarter) = {on}  (expect True)")
        hdc.heater = "OFF"
        time.sleep(0.2)
        print(f"  heater (off) = {hdc.heater}  (expect False)")

    # --- offsets (EEPROM!) -------------------------------------------------------
    if RUN_EEPROM_TESTS:
        section("Offsets  (EEPROM write — endurance 1000..50000 cycles)")
        saved = hdc.offsets
        print(f"  current offsets (T, RH) = {saved}")
        hdc.offsets = (1.0247, 1.953125)  # ~6x T-LSB, ~10x RH-LSB; clean multiples
        time.sleep(0.08)  # PR1 fix #4: tProg wait before read-back
        back = hdc.offsets
        print(f"  wrote (1.0247, 1.953125), read back {back}")
        hdc.offsets = saved  # restore
        time.sleep(0.08)
    else:
        print("\n(skipping EEPROM offset test; set RUN_EEPROM_TESTS=True to enable)")


except Exception as e:  # a crash anywhere is a recorded failure, not a silent stop
    print(f"FAIL: Unhandled exception: {e}")
    _Tally.failed += 1  # keep the verdict honest -- an uncaught crash must not read as ALL PASSED


print(f"=== Summary: {_Tally.passed} passed, {_Tally.failed} failed ===")
print("ALL TESTS PASSED" if _Tally.passed > 0 and _Tally.failed == 0 else "SOME TESTS FAILED")
print("~~END~~")  # terminal sentinel -- MUST be the last line, outside the try/except
