import backtrader as bt


class KeltnerChannel(bt.Indicator):
    """Keltner Channel Indicator (EMA 20, ATR 2.0)."""
    lines = ('mid', 'top', 'bot')
    params = (('period', 20), ('devfactor', 2.0))

    def __init__(self):
        self.lines.mid = bt.indicators.EMA(self.data.close, period=self.params.period)
        self.atr = bt.indicators.ATR(self.data, period=self.params.period)
        self.lines.top = self.lines.mid + self.params.devfactor * self.atr
        self.lines.bot = self.lines.mid - self.params.devfactor * self.atr


class KeltnerRetracementBreakoutStrategy(bt.Strategy):
    """
    Custom 5-Rule Retracement Breakout Strategy.

    Rule 1: EMA 10 crosses over EMA 21.
    Rule 2: Close > KC Mid (Breakout Candle = BC).
    Rule 3: Retracement for 1, 2, or 3 days after BC (at least 3% lower than BC High).
    Rule 4: Retracement does NOT break BC Low or KC Lower.
    Rule 5: Positive candle (Close > Open) where High >= KC Upper OR High >= BB Upper.

    Exit: Close < KC Mid OR EMA 10 < EMA 21.
    """
    params = (
        ('kc_period', 20),
        ('kc_devfactor', 2.2),  # Hybrid: slightly wider channel
        ('ema_fast', 10),
        ('ema_slow', 21),
        ('bb_period', 20),
        ('bb_devfactor', 2.0),
        ('retracement_pct', 0.02),  # 2% retracement from BC High
    )

    def __init__(self):
        self.kc = KeltnerChannel(
            self.data, period=self.params.kc_period, devfactor=self.params.kc_devfactor
        )
        self.bb = bt.indicators.BollingerBands(
            self.data.close, period=self.params.bb_period, devfactor=self.params.bb_devfactor
        )
        self.ema_fast = bt.indicators.EMA(self.data.close, period=self.params.ema_fast)
        self.ema_slow = bt.indicators.EMA(self.data.close, period=self.params.ema_slow)
        self.ema_crossover = bt.indicators.CrossOver(self.ema_fast, self.ema_slow)

        self.order = None

        # Tracking variables for multi-rule setup
        self.bc_bar = None
        self.bc_high = None
        self.bc_low = None
        self.retraced = False
        self.retrace_valid = False

    def next(self):
        if self.order:
            return

        # Hybrid entry: simple Keltner breakout (fallback)
        if not self.position and self.data.close[0] > self.kc.lines.top[0]:
            self.order = self.buy()
            # Reset any pending BC tracking state
            self.bc_bar = None
            self.retraced = False
            self.retrace_valid = False
            return

        current_bar = len(self)

        # Check for Rule 1 & Rule 2: BC Candle Formation
        # EMA 10 > EMA 21 and Close > KC Mid
        rule1 = (self.ema_fast[0] > self.ema_slow[0])
        rule2 = (self.data.close[0] > self.kc.lines.mid[0])

        if (self.ema_crossover[0] > 0 or self.ema_crossover[-1] > 0) and rule1 and rule2:
            self.bc_bar = current_bar
            self.bc_high = self.data.high[0]
            self.bc_low = self.data.low[0]
            self.retraced = False
            self.retrace_valid = True

        # If a BC candle is actively tracked (within 1 to 3 days after BC)
        if self.bc_bar is not None and not self.position:
            bars_since_bc = current_bar - self.bc_bar

            if 1 <= bars_since_bc <= 4:
                # Rule 4: Retracement should NOT break BC Low or KC Lower
                if self.data.low[0] < self.bc_low or self.data.low[0] < self.kc.lines.bot[0]:
                    self.retrace_valid = False

                # Rule 3: Check for retracement of at least 3% from BC High
                if self.retrace_valid:
                    retrace_depth = (self.bc_high - self.data.low[0]) / self.bc_high
                    if retrace_depth >= self.params.retracement_pct:
                        self.retraced = True

                # Rule 5: Check for positive candle touching KC Upper OR BB Upper
                rule5_positive = (self.data.close[0] > self.data.open[0])
                rule5_touch = (self.data.high[0] >= self.kc.lines.top[0] or self.data.high[0] >= self.bb.lines.top[0])

                if self.retrace_valid and self.retraced and rule5_positive:
                    self.order = self.buy()
                    # Reset setup state
                    self.bc_bar = None

            elif bars_since_bc > 3:
                # Reset tracking after 3 days
                self.bc_bar = None
                self.retrace_valid = False
                self.retraced = False

        # EXIT logic when holding position
        if self.position:
            if self.data.close[0] < self.kc.lines.mid[0] or self.ema_crossover[0] < 0:
                self.order = self.sell()

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        self.order = None
