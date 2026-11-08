"""Collect and evaluate a frozen, prospective BTCUSDT Spot strategy.

The script only downloads public candles. It never sends orders and does not
use API keys. Interim performance metrics are intentionally withheld.
"""

import csv
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import backtest_engine as engine
from config import (
    CFG,
    OPERATING_BUDGET,
    PROTECTED_RESERVE,
    RISK_BUDGET,
    WEEKLY_LOSS_LIMIT,
)


# Frozen protocol. Do not change these values during this validation.
SYMBOL = "BTCUSDT"
INTERVAL = "1h"
FAST_PERIOD = 50
SLOW_PERIOD = 200
ADX_PERIOD = 14
ADX_THRESHOLD = Decimal("30")
START_UTC = datetime(2026, 11, 1, 0, tzinfo=timezone.utc)
MINIMUM_DURATION = timedelta(days=365)
MINIMUM_CLOSED_TRADES = 20

CONTEXT_FILE = Path("data/BTCUSDT_1h_3y.csv")
FORWARD_FILE = Path("data/forward_validation_BTCUSDT_1h.csv")
FINAL_REPORT = Path("data/forward_validation_adx30_final.csv")
FINAL_TRADES = Path("data/forward_validation_adx30_trades.csv")

PUBLIC_BASE_URL = "https://data-api.binance.vision/api/v3/klines"
HOUR = timedelta(hours=1)
HOUR_MS = 60 * 60 * 1000
LIMIT = 1000
ZERO = Decimal("0")
ONE = Decimal("1")


def verify_frozen_configuration():
    expected = {
        "capital inicial": (CFG.initial_capital, Decimal("20.00")),
        "reserva protegida": (PROTECTED_RESERVE, Decimal("10.00")),
        "presupuesto operativo": (OPERATING_BUDGET, Decimal("10.00")),
        "riesgo por operación": (RISK_BUDGET, Decimal("0.40")),
        "límite semanal": (WEEKLY_LOSS_LIMIT, Decimal("2.00")),
        "comisión por lado": (CFG.fee_rate_per_side, Decimal("0.001")),
        "slippage por lado": (CFG.slippage_rate_per_side, Decimal("0.001")),
        "stop loss": (engine.STOP_LOSS_PCT, Decimal("0.02")),
        "coste ida y vuelta": (engine.ROUND_TRIP_COST, Decimal("0.004")),
    }

    changed = [
        name for name, (actual, target) in expected.items()
        if actual != target
    ]
    if changed:
        raise RuntimeError(
            "La validación está bloqueada porque cambiaron valores congelados: "
            + ", ".join(changed)
        )
    if not CFG.spot_only or CFG.live_trading_enabled or CFG.allow_withdrawals:
        raise RuntimeError(
            "La validación requiere Spot, trading real desactivado y retiros desactivados."
        )
    if CFG.max_open_positions != 1:
        raise RuntimeError("La validación requiere una sola posición simultánea.")


def parse_utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_forward_candles():
    if not FORWARD_FILE.exists():
        return {}

    result = {}
    with FORWARD_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        required = {"open_time_utc", "open", "high", "low", "close"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"Encabezados inesperados en {FORWARD_FILE}.")

        for row in reader:
            candle_time = parse_utc(row["open_time_utc"])
            if candle_time in result:
                raise ValueError(f"Vela duplicada en el archivo prospectivo: {candle_time}")
            candle = {
                "time": candle_time,
                "open": Decimal(row["open"]),
                "high": Decimal(row["high"]),
                "low": Decimal(row["low"]),
                "close": Decimal(row["close"]),
            }
            validate_candle(candle)
            result[candle_time] = candle
    return result


def validate_candle(candle):
    prices = (candle["open"], candle["high"], candle["low"], candle["close"])
    if any(not price.is_finite() or price <= ZERO for price in prices):
        raise ValueError(f"Precio inválido en {candle['time']}.")
    if (
        candle["high"] < max(candle["open"], candle["close"])
        or candle["low"] > min(candle["open"], candle["close"])
        or candle["high"] < candle["low"]
    ):
        raise ValueError(f"OHLC inconsistente en {candle['time']}.")


def first_missing_time(context_end, cached):
    expected = context_end + HOUR
    for candle_time in sorted(cached):
        if candle_time < expected:
            continue
        if candle_time > expected:
            return expected
        expected += HOUR
    return expected


def fetch_batch(start_ms, end_ms):
    params = urlencode({
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": LIMIT,
    })
    request = Request(
        f"{PUBLIC_BASE_URL}?{params}",
        headers={"User-Agent": "TradingBot-Forward-Validation/1.0"},
    )

    for attempt in range(5):
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, list):
                raise RuntimeError(f"Respuesta inesperada de Binance: {payload}")
            return payload
        except HTTPError as error:
            if error.code not in (418, 429) or attempt == 4:
                raise
            wait_seconds = int(error.headers.get("Retry-After", 5 * (attempt + 1)))
            print(f"Límite público alcanzado; esperando {wait_seconds} segundos.")
            time.sleep(wait_seconds)
        except (URLError, TimeoutError):
            if attempt == 4:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("No se pudo descargar el lote de velas.")


def download_closed_candles(context_end, cached, now):
    cursor_time = first_missing_time(context_end, cached)
    cursor_ms = int(cursor_time.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000) - 1
    newly_seen = 0

    while cursor_ms <= end_ms:
        batch = fetch_batch(cursor_ms, end_ms)
        if not batch:
            break

        encountered_open_candle = False
        latest_open_ms = None

        for row in batch:
            if len(row) < 7:
                raise ValueError("Binance devolvió una vela incompleta.")
            open_ms = int(row[0])
            close_ms = int(row[6])
            latest_open_ms = open_ms if latest_open_ms is None else max(latest_open_ms, open_ms)

            # Do not save an hour until Binance says the full candle has closed.
            if close_ms >= int(now.timestamp() * 1000):
                encountered_open_candle = True
                continue

            candle_time = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
            if candle_time <= context_end:
                continue

            candle = {
                "time": candle_time,
                "open": Decimal(row[1]),
                "high": Decimal(row[2]),
                "low": Decimal(row[3]),
                "close": Decimal(row[4]),
            }
            validate_candle(candle)
            if candle_time not in cached:
                newly_seen += 1
            cached[candle_time] = candle

        if encountered_open_candle or len(batch) < LIMIT:
            break
        if latest_open_ms is None or latest_open_ms < cursor_ms:
            raise RuntimeError("La descarga de velas no avanzó.")
        cursor_ms = latest_open_ms + HOUR_MS
        time.sleep(0.2)

    if newly_seen:
        save_forward_candles(cached)
    return newly_seen


def save_forward_candles(candles):
    FORWARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = FORWARD_FILE.with_suffix(FORWARD_FILE.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=["open_time_utc", "open", "high", "low", "close"],
        )
        writer.writeheader()
        for candle_time in sorted(candles):
            candle = candles[candle_time]
            writer.writerow({
                "open_time_utc": candle_time.isoformat(),
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
            })
    temporary.replace(FORWARD_FILE)


def verify_contiguous(context, forward):
    combined = context + sorted(forward, key=lambda candle: candle["time"])
    for previous, current in zip(combined, combined[1:]):
        if current["time"] - previous["time"] != HOUR:
            raise ValueError(
                "Hay un hueco o salto horario entre "
                f"{previous['time'].isoformat()} y {current['time'].isoformat()}. "
                "No se publicará ningún resultado hasta resolverlo."
            )
    return combined


def benchmark_drawdown(candles, start_index, end_index):
    first_open = candles[start_index]["open"]
    curve = [CFG.initial_capital]
    entry_cost = OPERATING_BUDGET * engine.ONE_SIDE_COST

    for candle in candles[start_index:end_index]:
        for price in (candle["high"], candle["low"]):
            marked_value = OPERATING_BUDGET * price / first_open
            exit_cost = marked_value * engine.ONE_SIDE_COST
            curve.append(PROTECTED_RESERVE + marked_value - entry_cost - exit_cost)

    peak = curve[0]
    worst = ZERO
    for equity in curve[1:]:
        peak = max(peak, equity)
        if peak > ZERO:
            worst = max(worst, (peak - equity) / peak)
    return worst


def profit_factor(trades, field):
    positive = sum((trade[field] for trade in trades if trade[field] > ZERO), ZERO)
    negative = abs(sum((trade[field] for trade in trades if trade[field] < ZERO), ZERO))
    return positive / negative if negative > ZERO else None


def write_final_reports(metrics, trades):
    FINAL_REPORT.parent.mkdir(parents=True, exist_ok=True)
    if FINAL_REPORT.exists() or FINAL_TRADES.exists():
        raise FileExistsError(
            "Ya existe el informe final. Se conserva para impedir reescribir "
            "o volver a consultar este holdout."
        )

    trade_fields = [
        "time", "reason", "entry", "exit", "size", "gross", "costs",
        "net", "capital", "weekly_loss_after_trade",
    ]
    with FINAL_TRADES.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=trade_fields)
        writer.writeheader()
        for trade in trades:
            writer.writerow({key: trade[key] for key in trade_fields})

    with FINAL_REPORT.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(metrics))
        writer.writeheader()
        writer.writerow(metrics)


def main():
    verify_frozen_configuration()
    if FINAL_REPORT.exists() or FINAL_TRADES.exists():
        raise FileExistsError("El informe final ya existe; no se volverá a evaluar.")
    if not CONTEXT_FILE.exists():
        raise FileNotFoundError(f"No se encontró el contexto histórico: {CONTEXT_FILE}")

    context = engine.load_candles(CONTEXT_FILE)
    context_end = context[-1]["time"]
    if context_end != datetime(2026, 10, 31, 3, tzinfo=timezone.utc):
        raise ValueError(
            "El último dato de contexto cambió. Se esperaba 2026-10-31 03:00 UTC; "
            f"se encontró {context_end.isoformat()}. No se continuará con un contexto distinto."
        )

    now = datetime.now(timezone.utc)
    cached = read_forward_candles()
    added = download_closed_candles(context_end, cached, now)
    forward = [cached[when] for when in sorted(cached)]

    print("=== VALIDACIÓN PROSPECTIVA CONGELADA ===")
    print("Par: BTCUSDT Spot | intervalo: 1h | sólo datos públicos")
    print(f"Estrategia fija: SMA {FAST_PERIOD}/{SLOW_PERIOD} + ADX {ADX_PERIOD} >= {ADX_THRESHOLD}")
    print(f"Inicio de evaluación UTC: {START_UTC.isoformat()}")
    print(f"Velas nuevas guardadas en esta ejecución: {added}")
    print(f"Archivo de velas prospectivas: {FORWARD_FILE}")
    print("API keys: no usadas | órdenes reales: no enviadas")

    if not forward:
        print("Aún no hay velas prospectivas cerradas; no se calcularon métricas.")
        return

    combined = verify_contiguous(context, forward)
    test_indexes = [
        index for index, candle in enumerate(combined)
        if candle["time"] >= START_UTC
    ]
    if not test_indexes:
        print("Aún no hay velas desde el inicio fijado; no se calcularon métricas.")
        return

    start_index = test_indexes[0]
    test_end = combined[-1]["time"] + HOUR
    if test_end - START_UTC < MINIMUM_DURATION:
        print(
            f"Recopilación en curso hasta al menos "
            f"{(START_UTC + MINIMUM_DURATION).isoformat()}; "
            "no se muestran métricas intermedias."
        )
        return

    original_fast = engine.FAST_PERIOD
    original_slow = engine.SLOW_PERIOD
    try:
        engine.FAST_PERIOD = FAST_PERIOD
        engine.SLOW_PERIOD = SLOW_PERIOD
        final_capital, trades, strategy_dd = engine.run_backtest(
            combined,
            start_index=start_index,
            end_index=len(combined),
            adx_period=ADX_PERIOD,
            adx_threshold=ADX_THRESHOLD,
        )
    finally:
        engine.FAST_PERIOD = original_fast
        engine.SLOW_PERIOD = original_slow

    if len(trades) < MINIMUM_CLOSED_TRADES:
        print(
            f"Se alcanzó un año, pero aún no hay {MINIMUM_CLOSED_TRADES} "
            "operaciones cerradas. Se seguirá recopilando sin mostrar métricas."
        )
        return

    net = final_capital - CFG.initial_capital
    gross = sum((trade["gross"] for trade in trades), ZERO)
    costs = sum((trade["costs"] for trade in trades), ZERO)
    wins = sum(1 for trade in trades if trade["net"] > ZERO)
    losses = sum(1 for trade in trades if trade["net"] < ZERO)
    benchmark = engine.run_buy_and_hold(combined, start_index, len(combined))
    benchmark_dd = benchmark_drawdown(combined, start_index, len(combined))

    metrics = {
        "strategy": f"SMA{FAST_PERIOD}/{SLOW_PERIOD}+ADX{ADX_PERIOD}>={ADX_THRESHOLD}",
        "start_utc": START_UTC.isoformat(),
        "end_utc_exclusive": test_end.isoformat(),
        "candles": len(combined) - start_index,
        "initial_capital": str(CFG.initial_capital),
        "final_capital": str(final_capital),
        "net_profit": str(net),
        "return_pct": str(net / CFG.initial_capital * Decimal("100")),
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": str(Decimal(wins) / Decimal(len(trades)) * Decimal("100")),
        "gross_pnl": str(gross),
        "total_costs": str(costs),
        "net_expectancy_per_trade": str(net / Decimal(len(trades))),
        "gross_profit_factor": str(profit_factor(trades, "gross")),
        "net_profit_factor": str(profit_factor(trades, "net")),
        "strategy_max_drawdown_pct": str(strategy_dd * Decimal("100")),
        "buy_hold_final_capital": str(benchmark),
        "buy_hold_net_profit": str(benchmark - CFG.initial_capital),
        "buy_hold_intrabar_drawdown_pct": str(benchmark_dd * Decimal("100")),
        "closed_trade_minimum": MINIMUM_CLOSED_TRADES,
        "data_source": "Binance public Spot klines; no authentication",
        "simulation_only": True,
    }
    write_final_reports(metrics, trades)

    print("=== VALIDACIÓN FINAL COMPLETADA ===")
    print(f"Resultado neto: ${net:+.4f}")
    print(f"Operaciones cerradas: {len(trades)} ({wins} ganadoras, {losses} perdedoras)")
    print(f"Costes totales: ${costs:.4f}")
    print(f"Expectativa neta por operación: ${net / Decimal(len(trades)):+.4f}")
    print(f"Profit Factor bruto: {metrics['gross_profit_factor']}")
    print(f"Profit Factor neto: {metrics['net_profit_factor']}")
    print(f"Drawdown estrategia: {strategy_dd * 100:.2f}%")
    print(f"Drawdown BTC intravela: {benchmark_dd * 100:.2f}%")
    print(f"Informe: {FINAL_REPORT}")
    print(f"Operaciones: {FINAL_TRADES}")
    print("Este informe final se guarda una sola vez; no reajustar parámetros con estos datos.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, ArithmeticError, URLError) as exc:
        print(f"ERROR en la validación prospectiva: {exc}")
        raise SystemExit(1)
