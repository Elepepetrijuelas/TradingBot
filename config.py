
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class BotConfig:
    # Capital y reserva
    initial_capital: Decimal = Decimal("20.00")
    protected_reserve_pct: Decimal = Decimal("0.50")

    # Riesgo
    risk_per_trade_pct: Decimal = Decimal("0.02")
    weekly_loss_limit_pct: Decimal = Decimal("0.10")

    # Costos estimados; verificar la tarifa real de la cuenta
    fee_rate_per_side: Decimal = Decimal("0.001")
    slippage_rate_per_side: Decimal = Decimal("0.001")

    # Exposición
    max_open_positions: int = 1
    max_position_pct_of_operating: Decimal = Decimal("1.00")

    # Seguridad
    spot_only: bool = True
    allow_withdrawals: bool = False
    live_trading_enabled: bool = False


CFG = BotConfig()

PROTECTED_RESERVE = (
    CFG.initial_capital * CFG.protected_reserve_pct
)

OPERATING_BUDGET = (
    CFG.initial_capital - PROTECTED_RESERVE
)

RISK_BUDGET = (
    CFG.initial_capital * CFG.risk_per_trade_pct
)

WEEKLY_LOSS_LIMIT = (
    CFG.initial_capital * CFG.weekly_loss_limit_pct
)


def print_config():
    print(f"Capital inicial: ${CFG.initial_capital}")
    print(f"Reserva protegida: ${PROTECTED_RESERVE}")
    print(f"Presupuesto operativo: ${OPERATING_BUDGET}")
    print(f"Riesgo presupuestado: ${RISK_BUDGET}")
    print(f"Parada semanal: ${WEEKLY_LOSS_LIMIT}")
    print(f"Trading real habilitado: {CFG.live_trading_enabled}")


if __name__ == "__main__":
    print_config()
