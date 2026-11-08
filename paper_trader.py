"""Paper trader for the frozen SMA50/200 + ADX14>=30 Spot strategy.

Only public Binance candles are requested. This module has no order API,
does not use API keys, and keeps its simulation state separate from the live
weekly guard state.
"""
import math
import os
import re
import tempfile
import argparse
import csv
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError

import backtest_engine as engine
import forward_validation_adx30 as data_feed
from config import CFG, WEEKLY_LOSS_LIMIT


DATA_FILE = Path(__file__).with_name("data") / "BTCUSDT_1h_3y.csv"
STATE_FILE = Path(__file__).with_name("data") / "paper_trader_state.json"
TRADES_FILE = Path(__file__).with_name("data") / "paper_trader_trades.csv"
PAPER_START = data_feed.START_UTC
POLL_SECONDS = 300
FAST_PERIOD = 50
SLOW_PERIOD = 200
ADX_PERIOD = 14
ADX_THRESHOLD = Decimal("30")
ZERO = Decimal("0")
ONE = Decimal("1")
HOUR = 60 * 60


DECIMAL_FIELDS = (
    "capital",
    "weekly_loss",
    "position_size",
    "entry_price",
    "stop_price",
    "peak_equity",
    "max_drawdown",
)

TRADE_FIELDS = (
    "time",
    "reason",
    "entry_time",
    "entry",
    "exit",
    "size",
    "gross",
    "costs",
    "net",
    "capital",
    "weekly_loss_after_trade",
)

STATE_FIELDS = {
    "version",
    "strategy",
    "last_processed_time",
    "capital",
    "weekly_loss",
    "week_id",
    "position_size",
    "entry_price",
    "stop_price",
    "entry_time",
    "in_position",
    "pending_entry",
    "pending_exit",
    "peak_equity",
    "max_drawdown",
    "trades",
}


def new_state():
    return {
        "version": 1,
        "strategy": "SMA50/200+ADX14>=30",
        "last_processed_time": PAPER_START.timestamp() - HOUR,
        "capital": CFG.initial_capital,
        "weekly_loss": ZERO,
        "week_id": None,
        "position_size": ZERO,
        "entry_price": ZERO,
        "stop_price": ZERO,
        "entry_time": None,
        "in_position": False,
        "pending_entry": False,
        "pending_exit": False,
        "peak_equity": CFG.initial_capital,
        "max_drawdown": ZERO,
        "trades": [],
    }


def _parse_utc_datetime(value, field_name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"Fecha inválida en {field_name}.")

    parsed = datetime.fromisoformat(value)

    if parsed.tzinfo is None:
        raise ValueError(f"La fecha {field_name} no tiene zona horaria.")

    return parsed.astimezone(timezone.utc)


def _validate_trade(trade, index):
    if not isinstance(trade, dict):
        raise ValueError(f"La operación {index} no es un objeto válido.")

    if set(trade) != set(TRADE_FIELDS):
        raise ValueError(f"Campos incorrectos en la operación {index}.")

    _parse_utc_datetime(trade["time"], f"trades[{index}].time")
    _parse_utc_datetime(trade["entry_time"], f"trades[{index}].entry_time")

    if not isinstance(trade["reason"], str) or not trade["reason"]:
        raise ValueError(f"Motivo inválido en la operación {index}.")

    decimal_trade_fields = (
        "entry",
        "exit",
        "size",
        "gross",
        "costs",
        "net",
        "capital",
        "weekly_loss_after_trade",
    )

    values = {}

    for name in decimal_trade_fields:
        try:
            value = Decimal(str(trade[name]))
        except Exception as exc:
            raise ValueError(
                f"Valor numérico inválido en operación {index}: {name}."
            ) from exc

        if not value.is_finite():
            raise ValueError(
                f"Valor no finito en operación {index}: {name}."
            )

        values[name] = value

    if (
        values["entry"] <= ZERO
        or values["exit"] <= ZERO
        or values["size"] <= ZERO
        or values["costs"] < ZERO
        or values["capital"] <= ZERO
        or values["weekly_loss_after_trade"] < ZERO
    ):
        raise ValueError(f"Valores incoherentes en la operación {index}.")


def load_state():
    if not STATE_FILE.exists():
        if TRADES_FILE.exists():
            raise RuntimeError(
                f"Existe {TRADES_FILE}, pero falta {STATE_FILE}. "
                "No iniciar un saldo nuevo automáticamente; revisar los archivos."
            )
        return new_state()

    try:
        with STATE_FILE.open("r", encoding="utf-8") as source:
            state = json.load(source)

        if not isinstance(state, dict):
            raise ValueError("El estado no contiene un objeto JSON.")

        if set(state) != STATE_FIELDS:
            raise ValueError("Los campos del estado no coinciden con la versión esperada.")

        if (
            state["version"] != 1
            or state["strategy"] != "SMA50/200+ADX14>=30"
        ):
            raise ValueError("Versión o estrategia del estado no reconocida.")

        for name in DECIMAL_FIELDS:
            try:
                state[name] = Decimal(str(state[name]))
            except Exception as exc:
                raise ValueError(f"Valor numérico inválido en {name}.") from exc

            if not state[name].is_finite():
                raise ValueError(f"Estado no finito en {name}.")

        if state["capital"] <= ZERO or state["weekly_loss"] < ZERO:
            raise ValueError("Capital o pérdida semanal inválidos.")

        if state["position_size"] < ZERO or state["entry_price"] < ZERO:
            raise ValueError("Tamaño o precio de posición inválido.")

        if state["stop_price"] < ZERO:
            raise ValueError("Stop inválido.")

        if state["peak_equity"] <= ZERO or state["max_drawdown"] < ZERO:
            raise ValueError("Equity máxima o drawdown inválido.")

        timestamp = state["last_processed_time"]

        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)
            or timestamp < 0
        ):
            raise ValueError("Último timestamp procesado inválido.")

        for name in ("in_position", "pending_entry", "pending_exit"):
            if not isinstance(state[name], bool):
                raise ValueError(f"El campo {name} debe ser booleano.")

        week_id = state["week_id"]

        if week_id is not None:
            if (
                not isinstance(week_id, str)
                or re.fullmatch(
                    r"\d{4}-W(?:0[1-9]|[1-4]\d|5[0-3])",
                    week_id,
                ) is None
            ):
                raise ValueError("Identificador de semana inválido.")

        if state["in_position"]:
            if (
                state["position_size"] <= ZERO
                or state["entry_price"] <= ZERO
                or state["stop_price"] <= ZERO
                or state["stop_price"] >= state["entry_price"]
            ):
                raise ValueError("Los datos de la posición abierta son incoherentes.")

            _parse_utc_datetime(state["entry_time"], "entry_time")

        else:
            if (
                state["position_size"] != ZERO
                or state["entry_price"] != ZERO
                or state["stop_price"] != ZERO
                or state["entry_time"] is not None
            ):
                raise ValueError("Hay datos de posición abierta cuando no existe posición.")

        if not isinstance(state["trades"], list):
            raise ValueError("Registro de operaciones inválido.")

        for index, trade in enumerate(state["trades"]):
            _validate_trade(trade, index)

        return state

    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"No se pudo validar {STATE_FILE}; el simulador se detuvo sin operar."
        ) from exc


def _atomic_write(path, writer_callback, newline=None):
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = None

    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )

        temporary_path = Path(temporary_name)

        with os.fdopen(
            file_descriptor,
            "w",
            encoding="utf-8",
            newline=newline,
        ) as output:
            writer_callback(output)
            output.flush()
            os.fsync(output.fileno())

        os.replace(temporary_path, path)

    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def save_state(state):
    serializable = dict(state)

    for name in DECIMAL_FIELDS:
        serializable[name] = str(serializable[name])

    def write_json(output):
        json.dump(
            serializable,
            output,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )

    _atomic_write(STATE_FILE, write_json)


def save_trade_ledger(trades):
    def write_csv(output):
        writer = csv.DictWriter(
            output,
            fieldnames=TRADE_FIELDS,
        )
        writer.writeheader()
        writer.writerows(trades)

    _atomic_write(TRADES_FILE, write_csv, newline="")



def make_trade_record(state, timestamp, exit_price, reason):
    size = state["position_size"]
    entry_price = state["entry_price"]
    gross = size * (exit_price / entry_price - ONE)
    costs = engine._estimated_round_trip_costs(
        size,
        entry_price,
        exit_price,
    )
    net = gross - costs
    state["capital"] += net
    if net < ZERO:
        state["weekly_loss"] += -net

    trade = {
        "time": timestamp.isoformat(),
        "reason": reason,
        "entry_time": state["entry_time"],
        "entry": str(entry_price),
        "exit": str(exit_price),
        "size": str(size),
        "gross": str(gross),
        "costs": str(costs),
        "net": str(net),
        "capital": str(state["capital"]),
        "weekly_loss_after_trade": str(state["weekly_loss"]),
    }
    state["trades"].append(trade)

    state["position_size"] = ZERO
    state["entry_price"] = ZERO
    state["stop_price"] = ZERO
    state["entry_time"] = None
    state["in_position"] = False
    return trade


def mark_equity(state, mark_price=None):
    equity = state["capital"]
    if state["in_position"] and mark_price is not None:
        equity = engine.equity_at_price(
            state["capital"],
            state["position_size"],
            state["entry_price"],
            mark_price,
        )

    if equity > state["peak_equity"]:
        state["peak_equity"] = equity
    if state["peak_equity"] > ZERO:
        decline = (state["peak_equity"] - equity) / state["peak_equity"]
        state["max_drawdown"] = max(state["max_drawdown"], decline)


def open_simulated_position(state, candle):
    size = engine.position_size_for_backtest(
        capital=state["capital"],
        weekly_loss=state["weekly_loss"],
        entry_price=candle["open"],
    )
    if size <= ZERO:
        return False

    state["position_size"] = size
    state["entry_price"] = candle["open"]
    state["stop_price"] = candle["open"] * (ONE - engine.STOP_LOSS_PCT)
    state["entry_time"] = candle["time"].isoformat()
    state["in_position"] = True
    return True


def process_candle(state, candles, closes, index, segment_starts, adx_values):
    candle = candles[index]
    timestamp = candle["time"]
    iso_week = timestamp.isocalendar()
    week_id = f"{iso_week.year}-W{iso_week.week:02d}"
    if week_id != state["week_id"]:
        state["week_id"] = week_id
        state["weekly_loss"] = ZERO

    events = []

    # Signal exits are filled at the next candle open.
    if state["in_position"] and state["pending_exit"]:
        trade = make_trade_record(state, timestamp, candle["open"], "Cruce bajista")
        events.append(f"salida por señal: ${Decimal(trade['net']):+.4f}")
        mark_equity(state)

    if state["in_position"]:
        if candle["open"] <= state["stop_price"]:
            mark_equity(state, candle["open"])
            trade = make_trade_record(state, timestamp, candle["open"], "Stop con gap")
            events.append(f"stop con gap: ${Decimal(trade['net']):+.4f}")
            mark_equity(state)
        elif candle["low"] <= state["stop_price"]:
            # Conservative OHLC ordering for drawdown: high, then stop.
            mark_equity(state, candle["high"])
            mark_equity(state, state["stop_price"])
            trade = make_trade_record(state, timestamp, state["stop_price"], "Stop loss")
            events.append(f"stop loss: ${Decimal(trade['net']):+.4f}")
            mark_equity(state)
        else:
            mark_equity(state, candle["high"])
            mark_equity(state, candle["low"])

    if (
        not state["in_position"]
        and state["pending_entry"]
        and state["weekly_loss"] < WEEKLY_LOSS_LIMIT
    ):
        if open_simulated_position(state, candle):
            events.append(f"entrada simulada a ${candle['open']}")
            if candle["low"] <= state["stop_price"]:
                mark_equity(state, candle["high"])
                mark_equity(state, state["stop_price"])
                trade = make_trade_record(
                    state,
                    timestamp,
                    state["stop_price"],
                    "Stop en vela de entrada",
                )
                events.append(f"stop en vela de entrada: ${Decimal(trade['net']):+.4f}")
                mark_equity(state)
            else:
                mark_equity(state, candle["high"])
                mark_equity(state, candle["low"])

    mark_equity(state, candle["close"])

    fast_now = engine._segmented_moving_average(
        closes, FAST_PERIOD, index, segment_starts
    )
    slow_now = engine._segmented_moving_average(
        closes, SLOW_PERIOD, index, segment_starts
    )
    fast_prev = (
        engine._segmented_moving_average(
            closes, FAST_PERIOD, index - 1, segment_starts
        )
        if index > 0 and segment_starts[index - 1] == segment_starts[index]
        else None
    )
    slow_prev = (
        engine._segmented_moving_average(
            closes, SLOW_PERIOD, index - 1, segment_starts
        )
        if index > 0 and segment_starts[index - 1] == segment_starts[index]
        else None
    )

    state["pending_entry"] = (
        fast_prev is not None
        and slow_prev is not None
        and fast_now is not None
        and slow_now is not None
        and fast_prev <= slow_prev
        and fast_now > slow_now
        and adx_values[index] is not None
        and adx_values[index] >= ADX_THRESHOLD
    )
    state["pending_exit"] = (
        fast_prev is not None
        and slow_prev is not None
        and fast_now is not None
        and slow_now is not None
        and fast_prev >= slow_prev
        and fast_now < slow_now
    )
    state["last_processed_time"] = timestamp.timestamp()
    return events


def process_new_candles(state, candles):
    last_processed = datetime.fromtimestamp(
        state["last_processed_time"], tz=timezone.utc
    )
    indexes = [
        index for index, candle in enumerate(candles)
        if PAPER_START <= candle["time"] and candle["time"] > last_processed
    ]
    if not indexes:
        return 0, []

    segment_starts = engine._build_segment_starts(candles)
    adx_values = engine.calculate_adx(candles, ADX_PERIOD)
    closes = [item["close"] for item in candles]
    processed = 0
    new_events = []

    for index in indexes:
        candle = candles[index]
        if index == 0 or candles[index - 1]["time"] + data_feed.HOUR != candle["time"]:
            raise ValueError(
                f"Hueco de datos antes de {candle['time'].isoformat()}; "
                "se detuvo el simulador sin procesar esta vela."
            )
        events = process_candle(
            state,
            candles,
            closes,
            index,
            segment_starts,
            adx_values,
        )
        save_state(state)
        processed += 1
        if events:
            new_events.append((candle["time"], events))

    return processed, new_events


def run_once(offline=False):
    data_feed.verify_frozen_configuration()
    if CFG.live_trading_enabled:
        raise RuntimeError("Trading real está habilitado; paper trader bloqueado.")

    context = engine.load_candles(DATA_FILE)
    context_end = context[-1]["time"]
    expected_end = datetime(2026, 10, 31, 3, tzinfo=timezone.utc)
    if context_end != expected_end:
        raise ValueError(
            f"Contexto inesperado: se esperaba {expected_end.isoformat()}, "
            f"se encontró {context_end.isoformat()}."
        )

    state = load_state()
    # El JSON es la fuente de verdad: reconstruir el CSV al iniciar.
    save_trade_ledger(state["trades"])
    cached = data_feed.read_forward_candles()
    added = 0
    if not offline:
        added = data_feed.download_closed_candles(
            context_end,
            cached,
            datetime.now(timezone.utc),
        )
    forward = [cached[when] for when in sorted(cached)]
    candles = data_feed.verify_contiguous(context, forward)
    processed, events = process_new_candles(state, candles)
    save_trade_ledger(state["trades"])

    print("=== PAPER TRADER BTCUSDT SPOT ===")
    print("Modo: simulación; no envía órdenes y no usa API keys.")
    print(f"Estrategia fija: SMA {FAST_PERIOD}/{SLOW_PERIOD} + ADX {ADX_PERIOD} >= {ADX_THRESHOLD}")
    print(f"Velas públicas nuevas descargadas: {added}")
    print(f"Velas cerradas procesadas ahora: {processed}")
    if candles:
        print(f"Última vela UTC: {candles[-1]['time'].isoformat()}")
    if events:
        for timestamp, descriptions in events:
            print(f"{timestamp.isoformat()} | " + " | ".join(descriptions))
    else:
        print("No hubo entradas ni salidas simuladas nuevas.")

    print(f"Operaciones cerradas: {len(state['trades'])}")
    print(f"Saldo simulado: ${state['capital']:.4f}")
    print(f"Pérdida semanal simulada: ${state['weekly_loss']:.4f} / ${WEEKLY_LOSS_LIMIT:.2f}")
    print(f"Drawdown máximo simulado: {state['max_drawdown'] * 100:.2f}%")
    print("Posición simulada abierta:", "Sí" if state["in_position"] else "No")
    print(f"Estado: {STATE_FILE}")
    print(f"Registro de operaciones: {TRADES_FILE}")


def main():
    parser = argparse.ArgumentParser(
        description="Simulador continuo Spot; no envía órdenes reales."
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="consultar velas públicas cada cinco minutos hasta Ctrl+C",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="procesar sólo las velas ya guardadas, sin conexión a Binance",
    )
    args = parser.parse_args()

    if not args.watch:
        run_once(offline=args.offline)
        return

    if args.offline:
        parser.error("--offline no se combina con --watch.")

    print("Paper trader activo. Ctrl+C lo detiene. No se envían órdenes reales.")
    while True:
        try:
            run_once()
        except HTTPError as exc:
            if exc.code not in (418, 429, 500, 502, 503, 504):
                raise
            print(f"Binance respondió HTTP {exc.code}; se reintentará en cinco minutos.")
        except (URLError, TimeoutError) as exc:
            print(f"Sin conexión a datos públicos ({exc}); se reintentará en cinco minutos.")
        except (OSError, ValueError, RuntimeError, KeyError, ArithmeticError) as exc:
            print(f"ERROR; el ciclo se detuvo sin órdenes: {exc}")
            raise SystemExit(1)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Paper trader detenido. No se enviaron órdenes reales.")
    except (OSError, ValueError, RuntimeError, KeyError, ArithmeticError) as exc:
        print(f"ERROR en paper trader: {exc}")
        raise SystemExit(1)
