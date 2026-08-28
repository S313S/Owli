"""§RP-1 阶段重放：以跑过的研究为底，只重跑出问题的那一段。

**重放不作关账证据。** 重放拿的是旧库旧产物，跑的是当下的代码，两者不同源；
它用来迭代与诊断（少付整跑的钱），关账仍然要一轮从规划开始的干净整跑。
谁把重放读数当整跑读数写进关账，账就是假的。
"""

from app.replay.sandbox import Fingerprint, ReplaySandbox, fingerprint

__all__ = ["Fingerprint", "ReplaySandbox", "fingerprint"]
