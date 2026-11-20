export const outcomes = ['COMPLIANT', 'VIOLATION', 'PENDING', 'ISOLATED'];
export const names = {COMPLIANT: '合规', VIOLATION: '违规', PENDING: '待证', ISOLATED: '隔离', BALANCED: '已勾稽',
  OPEN: '已受理', UPHELD: '维持原罚', WAIVED: '全额免除', REDUCED: '部分减免', NONE: '未发起',
  RECEIVED: '已收件', EMPTY_FILE: '空档', BLANK_FILE: '空行档', HEADER_ONLY: '仅表头',
  CLOSED: '本次入账', HELD: '留待复核', SETTLED: '此前已结', REVERSED: '追回冲账',
  PARTIAL: '部分留待'};
const dictionary = JSON.parse(document.getElementById('reason-map').textContent);
export const reason = code => code ? dictionary[code] || code : '—';
export const money = cents => (cents / 100).toFixed(2);

export function element(type, value, className) {
  const node = document.createElement(type);
  if (value !== undefined && value !== null) node.textContent = value;
  if (className) node.className = className;
  return node;
}

export function flag(code) {
  const node = element('span', names[code] || code, 'flag');
  node.dataset.status = code;
  return node;
}

export function matrix(root, snapshot) {
  const table = element('table', null, 'matrix');
  const head = table.createTHead().insertRow();
  for (const text of ['店组', ...outcomes.map(code => names[code])]) {
    const heading = element('th', text);
    heading.scope = 'col';
    head.append(heading);
  }
  const title = new Map(snapshot.coverage.strata.map(row => [row.code, row.title]));
  const body = table.createTBody();
  for (const group of snapshot.settlement.strata) {
    const row = body.insertRow();
    const name = element('th', title.get(group.code));
    name.scope = 'row';
    row.append(name);
    for (const code of outcomes) {
      const count = group.counts[code];
      const button = element('button', count, `cell ${code.toLowerCase()}`);
      button.type = 'button';
      button.disabled = !count;
      button.dataset.group = group.code;
      button.dataset.outcome = code;
      button.setAttribute('aria-label', `${title.get(group.code)} ${names[code]} ${count} 柜`);
      row.insertCell().append(button);
    }
  }
  root.replaceChildren(table);
}

export function chart(root, distribution) {
  const max = Math.max(1, ...distribution.map(row => row.target));
  const axis = element('div', null, 'axis');
  axis.append(element('span', '0'), element('span', `${max} 柜`));
  const result = [axis];
  for (const group of distribution) {
    const line = element('div', null, 'gap-row');
    const track = element('div', null, 'gap-track');
    const counts = {verified: group.verified, pending: group.pending, isolated: group.isolated, unfilled: group.unfilled};
    for (const [type, count] of Object.entries(counts)) {
      const segment = element('i', null, type);
      segment.style.width = `${count / max * 100}%`;
      if (count) track.append(segment);
    }
    track.setAttribute('role', 'img');
    track.setAttribute('aria-label', `${group.title} 已核 ${group.verified}，待证 ${group.pending}，隔离 ${group.isolated}，缺额 ${group.unfilled}`);
    const total = element('span', `缺 ${group.gap}`, 'num');
    total.dataset.gap = group.code;
    line.append(element('span', group.title), track, total);
    result.push(line);
  }
  root.replaceChildren(...result);
}

export function cases(root, rows) {
  const priority = {VIOLATION: 0, ISOLATED: 1, PENDING: 2, COMPLIANT: 3};
  const order = [...rows].sort((a, b) => priority[a.status] - priority[b.status] || b.original_cents - a.original_cents || a.key.localeCompare(b.key));
  const entries = order.map(row => {
    const button = element('button', null, 'case-item');
    button.type = 'button';
    button.dataset.key = row.key;
    const message = element('span', reason(row.reason));
    message.title = message.textContent;
    button.append(element('strong', row.key, 'num'), flag(row.status), message, element('span', money(row.original_cents), 'num'));
    return button;
  });
  root.replaceChildren(...(entries.length ? entries : [element('p', '本批暂无抽中柜位。', 'empty')]));
}

export function accounts(root, appeal) {
  const content = [['原罚额', appeal.original_cents], ['异议减免', appeal.reduction_cents], ['结算净罚额', appeal.net_cents]];
  root.replaceChildren(...content.map(([label, cents], index) => {
    const line = element('div', null, index === 2 ? 'money-row is-net' : 'money-row');
    line.append(element('span', label), element('strong', money(cents), 'num'));
    return line;
  }));
}

export function closing(root, record) {
  if (!record) {
    root.replaceChildren(element('p', '运行审计后核对待结金额。', 'empty'));
    return;
  }
  if (!record.rows.length) {
    root.replaceChildren(element('p', '本批次没有可结罚额。', 'empty'));
    return;
  }
  const box = element('div', null, 'closing-body');
  const totals = record.totals;
  const headline = element('div', null, 'closing-totals');
  const advance = record.revision > 1 || totals.previously_closed_cents !== 0;
  const cells = [advance ? ['累计已结', totals.closed_cents, 'closed'] : ['可结', totals.closed_cents, 'closed'],
    [advance ? '本次入账' : '本次结转', totals.close_cents, 'advance'], ['留待复核', totals.held_cents, 'held'],
    ['原罚额', totals.original_cents, 'original'], ['已裁定减免', totals.reduction_cents, 'reduction']];
  for (const [label, cents, kind] of cells) {
    const cell = element('div', null, `closing-total ${kind}`);
    cell.append(element('span', label), element('strong', money(cents), 'num'));
    headline.append(cell);
  }
  box.append(headline);

  const table = element('table', null, 'matrix closing-table');
  const head = table.createTHead().insertRow();
  for (const text of ['门店', '店组', '累计已结 · 元', '本次入账 · 元', '留待 · 元', '净额 · 元', '状态']) {
    const heading = element('th', text);
    heading.scope = 'col';
    head.append(heading);
  }
  const body = table.createTBody();
  for (const store of record.stores) {
    const row = body.insertRow();
    const name = element('th', `${store.store} ${store.title}`);
    name.scope = 'row';
    row.append(name, element('td', store.group_title),
      element('td', money(store.closed_cents), 'num'),
      element('td', money(store.close_cents), store.close_cents ? 'num closed-cell' : 'num'),
      element('td', money(store.held_cents), store.held_cents ? 'num held-cell' : 'num'),
      element('td', money(store.net_cents), 'num'), cellOf(flag(store.status)));
    row.title = `已结 ${store.closed_shelf_count} 柜 · 留待 ${store.held_shelf_count} 柜`;
  }
  const foot = body.insertRow();
  foot.className = 'closing-foot';
  foot.append(element('th', '合计'), element('td', `${record.stores.length} 店`),
    element('td', money(totals.closed_cents), 'num'),
    element('td', money(totals.close_cents), 'num'), element('td', money(totals.held_cents), 'num'),
    element('td', money(totals.net_cents), 'num'), element('td', `${totals.held_rows} 柜留待`));
  box.append(table);

  if (record.held.length) {
    const list = element('div', null, 'held-list');
    list.append(element('h3', '留待复核依据'));
    for (const row of record.held) {
      const line = element('div', null, 'held-item');
      line.append(element('strong', row.key, 'num'),
        element('span', `${row.rule_label} ${money(row.original_cents)} 元`),
        element('span', `异议版本 v${row.appeal_version} · ${names[row.appeal_state] || row.appeal_state}`),
        element('span', row.appeal_evidence_ref ? `依据 ${row.appeal_evidence_ref}` : '—', 'clip'));
      line.title = row.appeal_note || '';
      list.append(line);
    }
    box.append(list);
  }
  root.replaceChildren(box);
}

function cellOf(node) {
  const cell = document.createElement('td');
  cell.append(node);
  return cell;
}
