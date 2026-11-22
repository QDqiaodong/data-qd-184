"""共享常量与不可变金额校验工具。"""

FORMAT_VERSION = 1

MAX_AMOUNT = 10**12          # 单笔金额上限（整数分）。
MAX_TOTAL_CENTS = 10**18     # 多笔累加后的内部汇总上限。
MAX_BATCH_MONTHS = 12
MAX_INSTALLMENTS = 6
MAX_TERMS = 1000
MAX_SEQ = 1_000_000
MAX_BANK_ROWS_PER_MONTH = 240
CHARGEBACK_WINDOW = 2        # 到账 M 月只允许在 M、M+1、M+2 月被退扣。
