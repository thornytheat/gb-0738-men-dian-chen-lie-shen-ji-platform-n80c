import csv
import io
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import Response

from app.config import Options
from app.manifest import canonical
from app.modules.appeals.service import with_appeals
from app.modules.settlement.closing import plan_closing, preview_closing, statement, summary
from app.modules.settlement.service import run_audit
from app.pipeline import Audit

router = APIRouter(prefix='/api/settlement')
RunId = Annotated[str, Path(pattern=r'^[0-9a-f]{64}$')]
ClosingId = Annotated[str, Path(pattern=r'^[0-9a-f]{64}$')]


@router.get('')
def preview(request: Request, options: Annotated[Options, Query()]):
    return Audit(request.app.state.settings, options).settlement


@router.post('/runs')
def run(request: Request, options: Options):
    snapshot, reused = run_audit(request.app.state.settings, request.app.state.store, options)
    return {'reused': reused, **with_appeals(snapshot)}


@router.get('/runs/{run_id}')
def read_run(request: Request, run_id: RunId):
    return with_appeals(request.app.state.store.get(run_id))


@router.get('/runs/{run_id}/closings/preview')
def closing_preview(request: Request, run_id: RunId):
    store = request.app.state.store
    snapshot = store.get(run_id)
    return preview_closing(snapshot, store.read_closings(run_id)['published'])


@router.get('/runs/{run_id}/closings')
def closing_list(request: Request, run_id: RunId):
    history = request.app.state.store.read_closings(run_id)
    return {'run_id': run_id, 'latest': history['latest'],
            'versions': [summary(record) for record in history['published']]}


@router.post('/runs/{run_id}/closings')
def closing_publish(request: Request, run_id: RunId):
    record, reused = request.app.state.store.publish_closing(run_id, plan_closing)
    return {'reused': reused, **record}


@router.get('/runs/{run_id}/closings/{closing_id}')
def closing_read(request: Request, run_id: RunId, closing_id: ClosingId):
    return request.app.state.store.get_closing(run_id, closing_id)


@router.get('/runs/{run_id}/closings/{closing_id}/statement')
def closing_statement(request: Request, run_id: RunId, closing_id: ClosingId,
                      fmt: Annotated[Literal['json', 'csv'], Query(alias='format')] = 'json'):
    record = request.app.state.store.get_closing(run_id, closing_id)
    if fmt == 'csv':
        body, media = closing_csv(record), 'text/csv; charset=utf-8'
        name = f'shelf-closing-{record["revision"]}-{closing_id[:12]}.csv'
    else:
        body, media = canonical(statement(record)) + '\n', 'application/json'
        name = f'shelf-closing-{record["revision"]}-{closing_id[:12]}.json'
    return Response(body, media_type=media, headers={'Content-Disposition': f'attachment; filename="{name}"'})


def closing_csv(record):
    """逐柜结转单：同一行的原罚额、可结、留待与异议依据并列，便于财务追查。"""
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow(['结转版本', '批次', '账期', '门店', '门店名称', '店组', '柜位', '罚则', '处置',
                     '原罚额_分', '减免_分', '净额_分', '本次入账_分', '留待_分', '异议状态', '异议版本',
                     '异议提交号', '异议依据'])
    for row in record['rows']:
        writer.writerow([record['revision'], record['run_id'][:12], record['period']['version'],
                         row['store'], row['store_title'], row['group'], row['key'],
                         row['rule'], row['disposition_label'], row['original_cents'], row['reduction_cents'],
                         row['net_cents'], row['close_cents'], row['held_cents'], row['appeal_state'],
                         row['appeal_version'], row['appeal_request_id'] or '', row['appeal_evidence_ref'] or ''])
    totals = record['totals']
    writer.writerow(['合计', '', '', '', '', '', '', '', '本次入账', totals['original_cents'],
                     totals['reduction_cents'], totals['net_cents'], totals['close_cents'], totals['held_cents'],
                     '', '', '', ''])
    return '﻿' + buffer.getvalue()
