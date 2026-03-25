"""绝地潜兵2 随机战备生成器"""

import random

# ── 战备数据 ──

SUPPORT_WEAPONS = [
    "无后坐力炮", "机炮", "空爆火箭筒", "突击兵", "飞矛",
    "WASP发射器", "弹链榴弹", "M1000加特林", "C4背包", "焚燃者",
]

SUPPORT_WEAPONS_WITH_BACKPACK = [
    "唯一真旗", "火箭发射井", "灭菌器", "电锯", "电榴弹", "荡平者",
    "火次抛", "鱼叉", "纪元", "破门锤", "磁轨炮", "类星体",
    "电弧发射器", "MG206重机枪", "火焰发射器", "榴弹发射器",
    "反器材狙击枪", "激光大炮", "M105机枪", "次抛", "MG43机枪",
]

ORBITAL = [
    "轨道精准", "轨道火力网", "轨道毒气", "轨道120", "轨道380",
    "轨道空爆", "轨道烟雾", "轨道EMP", "轨道激光", "轨道凝固汽油弹",
    "轨道炮", "轨道游走",
]

EAGLE = [
    "飞鹰扫射", "飞鹰空气", "飞鹰集束", "飞鹰烟雾",
    "飞鹰凝固汽油弹", "飞鹰火箭巢", "500KG",
]

DEFENSIVE = [
    "反步兵地雷", "燃烧地雷", "反坦克地雷", "屏障发生器",
    "重机枪支架", "榴弹墙", "毒气地雷", "反坦克炮台",
]

TURRETS = [
    "小机枪炮台", "大机枪炮台", "机炮炮台", "迫击炮炮台",
    "火箭炮台", "特斯拉塔", "EMP迫击炮炮台", "激光炮台",
    "火焰炮台", "毒气迫击炮炮台",
]

BACKPACKS = [
    "补包", "跳包", "盾牌", "实弹狗", "激光狗", "蛋盾",
    "定向护盾", "火狗", "地狱火背包", "电狗", "悬浮包",
    "毒狗", "瞬移包",
]

VEHICLES = [
    "EXO45爱国者机甲", "EXO49解放者机甲", "侦查车", "坦克",
]

# 分类名称映射
CATEGORY_NAMES = {
    "support": "支援武器",
    "orbital": "轨道打击",
    "eagle": "飞鹰支援",
    "defensive": "阵地支援",
    "turret": "自动炮台",
    "backpack": "背包",
    "vehicle": "车辆/机甲",
}

# 所有战备汇总（用于全随机）
ALL_STRATAGEMS = (
    SUPPORT_WEAPONS + SUPPORT_WEAPONS_WITH_BACKPACK +
    ORBITAL + EAGLE + DEFENSIVE + TURRETS + BACKPACKS + VEHICLES
)

# 战备 -> 分类名 的反查表
_ITEM_TO_CATEGORY: dict[str, str] = {}
for _item in SUPPORT_WEAPONS:
    _ITEM_TO_CATEGORY[_item] = "支援武器"
for _item in SUPPORT_WEAPONS_WITH_BACKPACK:
    _ITEM_TO_CATEGORY[_item] = "支援武器(可携带背包)"
for _item in ORBITAL:
    _ITEM_TO_CATEGORY[_item] = "轨道打击"
for _item in EAGLE:
    _ITEM_TO_CATEGORY[_item] = "飞鹰支援"
for _item in DEFENSIVE:
    _ITEM_TO_CATEGORY[_item] = "阵地支援"
for _item in TURRETS:
    _ITEM_TO_CATEGORY[_item] = "自动炮台"
for _item in BACKPACKS:
    _ITEM_TO_CATEGORY[_item] = "背包"
for _item in VEHICLES:
    _ITEM_TO_CATEGORY[_item] = "车辆/机甲"


def get_category(item: str) -> str:
    """获取战备所属分类名称。"""
    return _ITEM_TO_CATEGORY.get(item, "未知")


def generate_random_loadout() -> list[str]:
    """按规则生成随机战备（4个槽位）。

    规则：
    - 70% 出1个支援武器，30% 不出但必出1个自动炮台
    - 可携带背包的支援武器被选中时，75% 概率额外获得1个背包（占槽）
    - 车辆/机甲：随机出现，最多1个
    - 背包：最多1个（含支援武器触发的）
    - 飞鹰：最多2个
    - 剩余槽位从非支援武器的所有分类中不重复填充
    """
    loadout: list[str] = []
    has_backpack = False

    # 第一步：决定支援武器
    if random.random() < 0.7:
        # 70% 概率带支援武器
        all_support = SUPPORT_WEAPONS + SUPPORT_WEAPONS_WITH_BACKPACK
        weapon = random.choice(all_support)
        loadout.append(weapon)

        # 如果是可携带背包的支援武器，75% 概率附带背包
        if weapon in SUPPORT_WEAPONS_WITH_BACKPACK and random.random() < 0.75:
            backpack = random.choice(BACKPACKS)
            loadout.append(backpack)
            has_backpack = True
    else:
        # 30% 概率不带支援武器，必出1个自动炮台
        turret = random.choice(TURRETS)
        loadout.append(turret)

    # 第二步：填充剩余槽位
    remaining = 4 - len(loadout)
    # 构建候选池（排除支援武器，排除已选的）
    candidates = ORBITAL + EAGLE + DEFENSIVE + TURRETS + BACKPACKS + VEHICLES
    candidates = [c for c in candidates if c not in loadout]

    # 跟踪分类计数
    eagle_count = sum(1 for item in loadout if item in EAGLE)
    vehicle_count = sum(1 for item in loadout if item in VEHICLES)
    backpack_count = 1 if has_backpack else 0

    for _ in range(remaining):
        if not candidates:
            break

        # 过滤掉违反限制的候选项
        valid = []
        for c in candidates:
            if c in EAGLE and eagle_count >= 2:
                continue
            if c in VEHICLES and vehicle_count >= 1:
                continue
            if c in BACKPACKS and backpack_count >= 1:
                continue
            valid.append(c)

        if not valid:
            break

        pick = random.choice(valid)
        loadout.append(pick)
        candidates.remove(pick)

        # 更新计数
        if pick in EAGLE:
            eagle_count += 1
        elif pick in VEHICLES:
            vehicle_count += 1
        elif pick in BACKPACKS:
            backpack_count += 1

    return loadout


def generate_full_random_loadout() -> list[str]:
    """完全随机：从所有战备大池子中随机抽4个不重复。"""
    return random.sample(ALL_STRATAGEMS, 4)


def format_loadout(loadout: list[str], full_random: bool = False) -> str:
    """将战备列表格式化为展示文本。"""
    title = "🎲 你的全随机战备：" if full_random else "🎲 你的随机战备："
    lines = [title, ""]
    for i, item in enumerate(loadout, 1):
        lines.append(f"  槽位{i}: {item}")
    return "\n".join(lines)
