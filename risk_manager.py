
from decimal import Decimal

from config import (
    CFG,
    PROTECTED_RESERVE,
    OPERATING_BUDGET,
    RISK_BUDGET,
    WEEKLY_LOSS_LIMIT,
)
from weekly_guard import load_state, trading_allowed


ZERO = Decimal("0")
TWO = Decimal("2")
WEEKLY_BUFFER = Decimal("0.0001")


def get_weekly_loss(manual_weekly_loss: Decimal) -> Decimal:
    """Combina las pérdidas manuales con el registro semanal guardado."""
    if (
        not manual_weekly_loss.is_finite()
        or manual_weekly_loss < ZERO
    ):
        raise ValueError("La pérdida semanal no es válida.")

    state = load_state()
    saved_weekly_loss = Decimal(state["weekly_loss"])

    return max(manual_weekly_loss, saved_weekly_loss)


def weekly_check(
    manual_weekly_loss: Decimal,
) -> tuple[bool, Decimal, str]:
    """Valida el registro persistente antes de permitir una operación."""
    try:
        weekly_loss = get_weekly_loss(manual_weekly_loss)

        if not trading_allowed():
            return False, weekly_loss, "Parada semanal activada."

        if weekly_loss >= WEEKLY_LOSS_LIMIT:
            return False, weekly_loss, "Parada semanal activada."

        return True, weekly_loss, "Control semanal aprobado."

    except (
        RuntimeError,
        ValueError,
        ArithmeticError,
        TypeError,
        KeyError,
    ):
        return (
            False,
            manual_weekly_loss,
            "ERROR: registro semanal inválido. Operación bloqueada.",
        )


def loss_fraction(
    entry_price: Decimal,
    stop_price: Decimal,
) -> Decimal:
    """Estima la pérdida por dólar invertido, incluidos los costes."""
    price_loss_fraction = (entry_price - stop_price) / entry_price

    costs_fraction = TWO * (
        CFG.fee_rate_per_side + CFG.slippage_rate_per_side
    )

    return price_loss_fraction + costs_fraction


def calculate_position_size(
    balance: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    weekly_loss: Decimal = ZERO,
) -> tuple[Decimal, Decimal, str]:
    """Calcula el tamaño de posición respetando los límites de riesgo."""

    if not all(
        value.is_finite()
        for value in (balance, entry_price, stop_price, weekly_loss)
    ):
        return ZERO, ZERO, "Los importes deben ser finitos."

    if balance <= ZERO or entry_price <= ZERO or stop_price <= ZERO:
        return ZERO, ZERO, "Balance y precios deben ser positivos."

    if weekly_loss < ZERO:
        return ZERO, ZERO, "La pérdida semanal no puede ser negativa."

    if stop_price >= entry_price:
        return ZERO, ZERO, "El stop debe estar por debajo de la entrada."

    allowed, weekly_loss, message = weekly_check(weekly_loss)
    if not allowed:
        return ZERO, ZERO, message

    available = min(
        OPERATING_BUDGET,
        max(ZERO, balance - PROTECTED_RESERVE),
    )

    max_position = min(
        available,
        OPERATING_BUDGET * CFG.max_position_pct_of_operating,
    )

    fraction = loss_fraction(entry_price, stop_price)

    if not fraction.is_finite() or fraction <= ZERO:
        return ZERO, ZERO, "No se pudo calcular un riesgo válido."

    remaining_weekly_limit = WEEKLY_LOSS_LIMIT - weekly_loss

    # Dejar un pequeño margen para no alcanzar el límite semanal.
    usable_weekly_limit = max(
        ZERO,
        remaining_weekly_limit - WEEKLY_BUFFER,
    )

    risk_limited_value = RISK_BUDGET / fraction
    weekly_limited_value = usable_weekly_limit / fraction

    position_value = min(
        max_position,
        risk_limited_value,
        weekly_limited_value,
    )

    if position_value <= ZERO:
        return ZERO, ZERO, "No hay presupuesto disponible."

    estimated_loss = position_value * fraction

    if weekly_loss + estimated_loss >= WEEKLY_LOSS_LIMIT:
        return (
            ZERO,
            ZERO,
            "No hay margen suficiente antes de la parada semanal.",
        )

    return (
        position_value,
        estimated_loss,
        "Tamaño calculado para simulación.",
    )


def check_trade(
    balance: Decimal,
    position_value: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    weekly_loss: Decimal = ZERO,
) -> tuple[bool, str]:
    """Valida una operación simulada antes de aprobarla."""

    if not all(
        value.is_finite()
        for value in (
            balance,
            position_value,
            entry_price,
            stop_price,
            weekly_loss,
        )
    ):
        return False, "Los importes deben ser finitos."

    if weekly_loss < ZERO:
        return False, "La pérdida semanal no puede ser negativa."

    allowed, weekly_loss, message = weekly_check(weekly_loss)
    if not allowed:
        return False, message

    if min(balance, position_value, entry_price, stop_price) <= ZERO:
        return False, "Importes y precios deben ser positivos."

    if stop_price >= entry_price:
        return False, "El stop debe estar por debajo de la entrada."

    available = min(
        OPERATING_BUDGET,
        max(ZERO, balance - PROTECTED_RESERVE),
    )

    if position_value > available:
        return False, "Orden rechazada: supera el presupuesto operativo."

    max_position = min(
        available,
        OPERATING_BUDGET * CFG.max_position_pct_of_operating,
    )

    if position_value > max_position:
        return False, "Orden rechazada: supera la exposición permitida."

    fraction = loss_fraction(entry_price, stop_price)
    estimated_loss = position_value * fraction

    if not estimated_loss.is_finite():
        return False, "Riesgo calculado inválido."

    if estimated_loss > RISK_BUDGET:
        return False, (
            f"Riesgo estimado ${estimated_loss:.4f} "
            f"supera el límite ${RISK_BUDGET:.4f}."
        )

    if weekly_loss + estimated_loss >= WEEKLY_LOSS_LIMIT:
        return False, "La operación podría alcanzar la parada semanal."

    return True, (
        "Operación aceptada para simulación. "
        f"Pérdida estimada: ${estimated_loss:.4f}"
    )


if __name__ == "__main__":
    entry = Decimal("100")
    stop = Decimal("95")
    weekly_loss = Decimal("0.00")
    balance = Decimal("20.00")

    position, estimated_loss, message = calculate_position_size(
        balance=balance,
        entry_price=entry,
        stop_price=stop,
        weekly_loss=weekly_loss,
    )

    print("=== CÁLCULO AUTOMÁTICO ===")
    print(message)
    print(f"Valor de posición: ${position:.4f}")
    print(f"Pérdida estimada: ${estimated_loss:.4f}")

    print("\n=== VALIDACIÓN ===")

    if position <= ZERO:
        print("Resultado: RECHAZADA")
        print(message)
    else:
        approved, check_message = check_trade(
            balance=balance,
            position_value=position,
            entry_price=entry,
            stop_price=stop,
            weekly_loss=weekly_loss,
        )

        print("Resultado:", "APROBADA" if approved else "RECHAZADA")
        print(check_message)
