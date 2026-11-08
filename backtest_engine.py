
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from config import (
    CFG,
    PROTECTED_RESERVE,
    OPERATING_BUDGET,
    RISK_BUDGET,
    WEEKLY_LOSS_LIMIT,
)

FAST_PERIOD = 50
SLOW_PERIOD = 200
STOP_LOSS_PCT = Decimal("0.02")
ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")
WEEKLY_BUFFER = Decimal("0.0001")

ONE_SIDE_COST = CFG.fee_rate_per_side + CFG.slippage_rate_per_side
ROUND_TRIP_COST = TWO * ONE_SIDE_COST


def dec(value):
    return Decimal(str(value).strip())


def parse_timestamp(value):
    value = value.strip()

    if value.replace(".", "", 1).isdigit():
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)

    result = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)

    return result.astimezone(timezone.utc)


def load_candles(data_file):
    try:
        file = open(data_file, "r", newline="", encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"No encuentro el archivo de datos: {data_file}"
        ) from exc

    with file:
        reader = csv.DictReader(file)

        if not reader.fieldnames:
            raise ValueError("El CSV no tiene encabezados.")

        names = {
            name.strip().lower(): name
            for name in reader.fieldnames
            if name
        }

        time_col = next(
            (
                names[name]
                for name in (
                    "open_time_utc",
                    "open_time",
                    "timestamp",
                    "datetime",
                    "date",
                    "time",
                )
                if name in names
            ),
            None,
        )

        required = ("open", "high", "low", "close")
        missing = [name for name in required if name not in names]

        if time_col is None or missing:
            raise ValueError(
                "No encuentro las columnas necesarias.\n"
                f"Encabezados: {reader.fieldnames}\n"
                f"Columna de tiempo: {time_col}\n"
                f"Columnas faltantes: {missing}"
            )

        candles = []
        now = datetime.now(timezone.utc)

        for row in reader:
            try:
                timestamp = parse_timestamp(row[time_col])

                candle = {
                    "time": timestamp,
                    "open": dec(row[names["open"]]),
                    "high": dec(row[names["high"]]),
                    "low": dec(row[names["low"]]),
                    "close": dec(row[names["close"]]),
                }

                if any(
                    candle[key] <= ZERO
                    for key in ("open", "high", "low", "close")
                ):
                    continue

                if (
                    candle["high"] < candle["low"]
                    or candle["high"] < max(
                        candle["open"], candle["close"]
                    )
                    or candle["low"] > min(
                        candle["open"], candle["close"]
                    )
                ):
                    continue

            except (
                ValueError,
                TypeError,
                KeyError,
                OverflowError,
                ArithmeticError,
            ):
                continue

            if timestamp + timedelta(hours=1) > now:
                continue

            candles.append(candle)

    candles.sort(key=lambda candle: candle["time"])

    unique = {}
    for candle in candles:
        unique[candle["time"]] = candle

    candles = sorted(
        unique.values(),
        key=lambda candle: candle["time"],
    )

    if len(candles) <= SLOW_PERIOD + 1:
        raise ValueError(
            f"Solo hay {len(candles)} velas válidas y cerradas. "
            "Se necesitan más datos."
        )

    return candles


def moving_average(values, period, index):
    if index < 0 or index + 1 < period:
        return None

    return sum(
        values[index - period + 1:index + 1]
    ) / Decimal(period)


def _calculate_adx_contiguous(candles, period):
    """Calcula ADX de Wilder sobre una secuencia sin huecos horarios."""
    count = len(candles)
    result = [None] * count

    if count < period * 2:
        return result

    period_decimal = Decimal(period)
    hundred = Decimal("100")
    true_ranges = [None] * count
    plus_dm = [ZERO] * count
    minus_dm = [ZERO] * count

    for i in range(1, count):
        current = candles[i]
        previous = candles[i - 1]
        upward = current["high"] - previous["high"]
        downward = previous["low"] - current["low"]

        if upward > downward and upward > ZERO:
            plus_dm[i] = upward
        if downward > upward and downward > ZERO:
            minus_dm[i] = downward

        true_ranges[i] = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )

    smoothed_tr = sum(true_ranges[1:period + 1], ZERO)
    smoothed_plus = sum(plus_dm[1:period + 1], ZERO)
    smoothed_minus = sum(minus_dm[1:period + 1], ZERO)
    dx_values = [None] * count

    for i in range(period, count):
        if i > period:
            smoothed_tr = smoothed_tr - smoothed_tr / period_decimal + true_ranges[i]
            smoothed_plus = smoothed_plus - smoothed_plus / period_decimal + plus_dm[i]
            smoothed_minus = smoothed_minus - smoothed_minus / period_decimal + minus_dm[i]

        if smoothed_tr == ZERO:
            dx_values[i] = ZERO
        else:
            plus_di = hundred * smoothed_plus / smoothed_tr
            minus_di = hundred * smoothed_minus / smoothed_tr
            di_sum = plus_di + minus_di
            dx_values[i] = ZERO if di_sum == ZERO else hundred * abs(plus_di - minus_di) / di_sum

        if i == (period * 2) - 1:
            result[i] = sum(dx_values[period:i + 1], ZERO) / period_decimal
        elif i > (period * 2) - 1:
            result[i] = (
                result[i - 1] * (period_decimal - ONE) + dx_values[i]
            ) / period_decimal

    return result


def calculate_adx(candles, period=14):
    """Calcula ADX de Wilder y reinicia su cálculo después de cada hueco horario."""
    if not isinstance(period, int) or isinstance(period, bool) or period <= 0:
        raise ValueError("El período ADX debe ser un entero positivo.")

    result = [None] * len(candles)
    segment_start = 0

    for i in range(1, len(candles) + 1):
        at_end = i == len(candles)
        has_gap = (
            not at_end
            and candles[i]["time"] - candles[i - 1]["time"] != timedelta(hours=1)
        )

        if at_end or has_gap:
            segment = candles[segment_start:i]
            segment_values = _calculate_adx_contiguous(segment, period)
            result[segment_start:i] = segment_values
            segment_start = i

    return result


def _build_segment_starts(candles):
    """Índice de inicio del tramo horario continuo al que pertenece cada vela."""
    starts = [0] * len(candles)
    current_start = 0

    for i in range(len(candles)):
        if i > 0 and candles[i]["time"] - candles[i - 1]["time"] != timedelta(hours=1):
            current_start = i
        starts[i] = current_start

    return starts


def _segmented_moving_average(values, period, index, segment_starts):
    if period <= 0:
        raise ValueError("El período de la media móvil debe ser positivo.")

    segment_start = segment_starts[index]
    if index - segment_start + 1 < period:
        return None

    return sum(values[index - period + 1:index + 1]) / Decimal(period)


def _estimated_round_trip_costs(position_size, entry_price, exit_price):
    """Estima costes por lado sobre el nominal real de entrada y salida."""
    one_side_cost = ROUND_TRIP_COST / TWO
    exit_notional = position_size * exit_price / entry_price
    return (position_size + exit_notional) * one_side_cost


def position_size_for_backtest(capital, weekly_loss, entry_price):
    if (
        not capital.is_finite()
        or not weekly_loss.is_finite()
        or not entry_price.is_finite()
        or capital <= ZERO
        or weekly_loss < ZERO
        or entry_price <= ZERO
    ):
        return ZERO

    if weekly_loss >= WEEKLY_LOSS_LIMIT:
        return ZERO

    available = min(
        OPERATING_BUDGET,
        max(ZERO, capital - PROTECTED_RESERVE),
    )

    max_position = min(
        available,
        OPERATING_BUDGET * CFG.max_position_pct_of_operating,
    )

    stop_price = entry_price * (ONE - STOP_LOSS_PCT)
    stop_costs_per_dollar = _estimated_round_trip_costs(
        ONE,
        entry_price,
        stop_price,
    )
    loss_fraction = STOP_LOSS_PCT + stop_costs_per_dollar

    remaining_weekly = max(
        ZERO,
        WEEKLY_LOSS_LIMIT - weekly_loss - WEEKLY_BUFFER,
    )

    if (
        available <= ZERO
        or max_position <= ZERO
        or loss_fraction <= ZERO
        or remaining_weekly <= ZERO
    ):
        return ZERO

    return min(
        max_position,
        RISK_BUDGET / loss_fraction,
        remaining_weekly / loss_fraction,
    )


def equity_at_price(capital, position_size, entry_price, mark_price):
    if position_size <= ZERO:
        return capital

    unrealized = position_size * (
        mark_price / entry_price - ONE
    )

    estimated_round_trip_costs = _estimated_round_trip_costs(
        position_size,
        entry_price,
        mark_price,
    )

    return capital + unrealized - estimated_round_trip_costs


def _validate_range(candles, start_index, end_index):
    if not isinstance(start_index, int) or isinstance(start_index, bool):
        raise ValueError("start_index debe ser un número entero.")

    if not isinstance(end_index, int) or isinstance(end_index, bool):
        raise ValueError("end_index debe ser un número entero.")

    if start_index < 0:
        raise ValueError("start_index no puede ser negativo.")

    if end_index > len(candles):
        raise ValueError("end_index supera la cantidad de velas disponibles.")

    if end_index <= start_index:
        raise ValueError("end_index debe ser mayor que start_index.")

    if end_index <= SLOW_PERIOD:
        raise ValueError(
            f"El intervalo debe terminar después de {SLOW_PERIOD} velas "
            "para poder calcular la media móvil lenta."
        )


def run_backtest(
    candles,
    start_index=SLOW_PERIOD,
    end_index=None,
    trend_filter_period=None,
    adx_period=None,
    adx_threshold=Decimal("25"),
):
    if end_index is None:
        end_index = len(candles)

    _validate_range(candles, start_index, end_index)

    if adx_period is not None:
        if (
            not isinstance(adx_period, int)
            or isinstance(adx_period, bool)
            or adx_period <= 0
        ):
            raise ValueError("adx_period debe ser un entero positivo.")

    adx_threshold = dec(adx_threshold)

    first_index = start_index
    closes = [candle["close"] for candle in candles]
    segment_starts = _build_segment_starts(candles)

    adx_values = (
        calculate_adx(candles, adx_period)
        if adx_period is not None
        else None
    )

    capital = CFG.initial_capital
    weekly_loss = ZERO
    current_week = None

    position_size = ZERO
    entry_price = ZERO
    stop_price = ZERO
    in_position = False

    trades = []
    peak_equity = capital
    max_drawdown = ZERO

    def update_drawdown(mark_price=None):
        nonlocal peak_equity, max_drawdown

        equity = capital

        if in_position and mark_price is not None:
            equity = equity_at_price(
                capital,
                position_size,
                entry_price,
                mark_price,
            )

        if equity > peak_equity:
            peak_equity = equity

        if peak_equity > ZERO:
            drawdown = (peak_equity - equity) / peak_equity
            max_drawdown = max(max_drawdown, drawdown)

    def close_position(exit_price, timestamp, reason):
        nonlocal capital, weekly_loss
        nonlocal position_size, entry_price, stop_price, in_position

        gross_pnl = position_size * (
            exit_price / entry_price - ONE
        )
        costs = _estimated_round_trip_costs(
            position_size,
            entry_price,
            exit_price,
        )
        net_pnl = gross_pnl - costs

        capital += net_pnl

        if net_pnl < ZERO:
            weekly_loss += -net_pnl

        trades.append({
            "time": timestamp,
            "reason": reason,
            "entry": entry_price,
            "exit": exit_price,
            "size": position_size,
            "gross": gross_pnl,
            "costs": costs,
            "net": net_pnl,
            "capital": capital,
            "weekly_loss_after_trade": weekly_loss,
        })

        position_size = ZERO
        entry_price = ZERO
        stop_price = ZERO
        in_position = False

    def open_position(price):
        nonlocal position_size, entry_price, stop_price, in_position

        size = position_size_for_backtest(
            capital=capital,
            weekly_loss=weekly_loss,
            entry_price=price,
        )

        if size <= ZERO:
            return

        position_size = size
        entry_price = price
        stop_price = entry_price * (ONE - STOP_LOSS_PCT)
        in_position = True

    pending_entry = False
    pending_exit = False

    for i in range(first_index, end_index):
        candle = candles[i]
        timestamp = candle["time"]

        # No se permite mantener una operación abierta atravesando horas sin datos.
        # Se liquida al cierre de la última vela conocida antes del hueco.
        if i > first_index and timestamp - candles[i - 1]["time"] != timedelta(hours=1):
            if in_position:
                previous = candles[i - 1]
                close_position(
                    previous["close"],
                    previous["time"],
                    "Cierre por hueco de datos",
                )
                update_drawdown()

            # Una señal pendiente no puede ejecutarse al otro lado del hueco.
            pending_entry = False
            pending_exit = False

        iso_week = timestamp.isocalendar()
        week_key = (iso_week.year, iso_week.week)

        if week_key != current_week:
            current_week = week_key
            weekly_loss = ZERO

        if in_position and pending_exit:
            close_position(candle["open"], timestamp, "Cruce bajista")
            update_drawdown()

        if in_position:
            if candle["open"] <= stop_price:
                update_drawdown(candle["open"])
                close_position(candle["open"], timestamp, "Stop con gap")
                update_drawdown()

            elif candle["low"] <= stop_price:
                update_drawdown(candle["high"])
                update_drawdown(stop_price)
                close_position(stop_price, timestamp, "Stop loss")
                update_drawdown()

            else:
                update_drawdown(candle["high"])
                update_drawdown(candle["low"])

        if (
            not in_position
            and pending_entry
            and weekly_loss < WEEKLY_LOSS_LIMIT
        ):
            open_position(candle["open"])

            if in_position and candle["low"] <= stop_price:
                update_drawdown(candle["high"])
                update_drawdown(stop_price)
                close_position(
                    stop_price,
                    timestamp,
                    "Stop en vela de entrada",
                )
                update_drawdown()
            elif in_position:
                update_drawdown(candle["high"])
                update_drawdown(candle["low"])

        update_drawdown(candle["close"])

        fast_now = _segmented_moving_average(
            closes, FAST_PERIOD, i, segment_starts
        )
        slow_now = _segmented_moving_average(
            closes, SLOW_PERIOD, i, segment_starts
        )
        fast_prev = (
            _segmented_moving_average(closes, FAST_PERIOD, i - 1, segment_starts)
            if i > 0 and segment_starts[i - 1] == segment_starts[i]
            else None
        )
        slow_prev = (
            _segmented_moving_average(closes, SLOW_PERIOD, i - 1, segment_starts)
            if i > 0 and segment_starts[i - 1] == segment_starts[i]
            else None
        )

        trend_average = None

        if trend_filter_period is not None:
            trend_average = _segmented_moving_average(
                closes,
                trend_filter_period,
                i,
                segment_starts,
            )

        pending_entry = (
            fast_prev is not None
            and slow_prev is not None
            and fast_prev <= slow_prev
            and fast_now > slow_now
            and (
                trend_filter_period is None
                or (
                    trend_average is not None
                    and candle["close"] > trend_average
                )
            )
            and (
                adx_period is None
                or (
                    adx_values[i] is not None
                    and adx_values[i] >= adx_threshold
                )
            )
        )

        pending_exit = (
            fast_prev is not None
            and slow_prev is not None
            and fast_prev >= slow_prev
            and fast_now < slow_now
        )

    if in_position:
        last = candles[end_index - 1]
        close_position(
            last["close"],
            last["time"],
            "Cierre final de prueba",
        )
        update_drawdown()

    return capital, trades, max_drawdown


def run_buy_and_hold(candles, start_index=0, end_index=None):
    if end_index is None:
        end_index = len(candles)

    _validate_range(candles, start_index, end_index)

    start_price = candles[start_index]["open"]
    end_price = candles[end_index - 1]["close"]
    invested = OPERATING_BUDGET

    gross_value = invested * (end_price / start_price)

    entry_cost = invested * ONE_SIDE_COST
    exit_cost = gross_value * ONE_SIDE_COST

    return (
        PROTECTED_RESERVE
        + gross_value
        - entry_cost
        - exit_cost
    )
