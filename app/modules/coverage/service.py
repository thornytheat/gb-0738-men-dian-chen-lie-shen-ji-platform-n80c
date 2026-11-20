from app.modules.coverage.sampling import choose_slots

PENDING_REASONS = {'MISSING_PHOTO', 'SCAN_INCOMPLETE', 'VISIT_MISSING'}


def coverage(intake, plans, photos, size):
    picked, quotas = choose_slots(intake, size)
    decisions = {(row['store'], row['slot']): row for row in plans['decisions']}
    bindings = {(row['visit'], row['slot']): row for row in photos['bindings']}
    slots = []
    for picked_row in picked:
        decision = decisions[picked_row['store'], picked_row['slot']]
        visit = decision['visit']
        plan = decision['plan']
        binding = bindings.get((visit['visit'], picked_row['slot'])) if visit else None
        if plan is None:
            reason = decision['reason']
        elif binding is None:
            reason = 'MISSING_PHOTO'
        else:
            reason = binding['reason']
        if reason is None:
            status = 'VERIFIED'
        elif reason in PENDING_REASONS:
            status = 'PENDING'
        else:
            status = 'ISOLATED'
        slots.append({**picked_row, 'visit': visit, 'plan': plan, 'plan_candidates': decision['candidates'],
                      'evidence': binding, 'evidence_status': status, 'reason': reason})
    strata = []
    for group in quotas:
        group_rows = [row for row in slots if row['group'] == group['code']]
        verified = sum(row['evidence_status'] == 'VERIFIED' for row in group_rows)
        pending = sum(row['evidence_status'] == 'PENDING' for row in group_rows)
        isolated = sum(row['evidence_status'] == 'ISOLATED' for row in group_rows)
        strata.append({**group, 'verified': verified, 'pending': pending, 'isolated': isolated,
                       'gap': group['target'] - verified})
    return {'target': size, 'selected': len(slots), 'verified': sum(row['evidence_status'] == 'VERIFIED' for row in slots),
            'unfilled': sum(row['unfilled'] for row in strata), 'slots': slots, 'strata': strata,
            'quarantine': photos['quarantine']}
