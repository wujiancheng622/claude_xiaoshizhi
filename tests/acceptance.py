"""把 unittest 测试方法映射到 02 测试设计的用例编号（G01-01…G19-26）。"""


def case(*ids: str):
    def deco(fn):
        fn.case_ids = tuple(ids)
        return fn
    return deco
