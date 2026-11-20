from hashlib import sha256

from app.modules.intake.values import identifier


def apportion(groups, size):
    """按店组权重以最大余数分配目标；余数并列按组编号先后。"""
    total = sum(int(group['weight']) for group in groups)
    quotas = {group['code']: size * int(group['weight']) // total for group in groups}
    remainders = {group['code']: size * int(group['weight']) % total for group in groups}
    for code in sorted(remainders, key=lambda item: (-remainders[item], item))[:size - sum(quotas.values())]:
        quotas[code] += 1
    return quotas


def score(seed, store, slot):
    return sha256(f'{seed}\0{store}\0{slot}'.encode()).hexdigest()


def choose_slots(intake, size):
    """按店组分配目标，在组内按确定性摘要排序抽取柜位。"""
    campaign = intake['campaign']
    groups = {row['store']: row['group'] for row in intake['stores']}
    quotas = apportion(campaign['groups'], size)
    rows, strata = [], []
    for group in campaign['groups']:
        code = group['code']
        pool = []
        for slot in intake['slots']:
            if groups.get(slot['store']) != code:
                continue
            key = f"{slot['store']}:{slot['slot']}"
            pool.append({'key': key, 'store': slot['store'], 'slot': slot['slot'], 'group': code,
                         'category': slot['category'], 'source': slot['source'],
                         'score': score(campaign['seed'], slot['store'], slot['slot'])})
        ranked = sorted(pool, key=lambda row: (row['score'], row['key']))
        target = quotas[code]
        selected = ranked[:target]
        rows.extend(selected)
        strata.append({'code': code, 'title': group['title'], 'weight': int(group['weight']), 'target': target,
                       'pool': len(pool), 'selected': len(selected), 'unfilled': max(0, target - len(pool)),
                       'ranked_candidates': ranked})
    return rows, strata


def slot_key(store, slot):
    return f'{identifier(store)}:{identifier(slot)}'
