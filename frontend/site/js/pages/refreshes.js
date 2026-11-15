import { api } from '../api.js';
import { ENUMS, can } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, filterBar, fmtTime, impactRows, kv, table, toast } from '../ui.js';

const state = { status: '', period: '', kind: 'metric', ref: '' };

function toneFor(status) {
  return { completed: 'ok', failed: 'danger', running: 'warn', pending: 'pending', skipped: 'info' }[status] || 'info';
}

function waveStatusBadge(status) {
  return `<span class="badge ${toneFor(status)}">${esc(ENUMS.waveStatus[status] || status)}</span>`;
}

function openBatch(view, ctx, item) {
  const waves = item.waves || [];
  const isolated = item.isolated || [];
  drawer({
    title: `${item.refresh_no} · ${item.period} 月结刷新`,
    subtitle: `状态 ${ENUMS.refreshStatus[item.status] || item.status} · 变更点 ${(item.change_points || []).length} 个`,
    body: `
      ${kv([
        ['刷新号', `<span class="mono">${esc(item.refresh_no)}</span>`],
        ['周期', esc(item.period)],
        ['状态', `<span class="badge ${toneFor(item.status)}">${esc(ENUMS.refreshStatus[item.status] || item.status)}</span>`],
        ['触发 / 操作人', `${esc(ENUMS.trigger[item.trigger] || item.trigger)} · ${esc(item.operator_email)}`],
        ['变更点', item.change_points.map((p) => `<span class="chip">${esc(ENUMS.changePointKind[p.kind] || p.kind)} · <span class="mono">${esc(p.ref)}</span></span>`).join(' ') || '—'],
        ['下游闭包', `${esc(item.summary.closure_count ?? 0)} 个指标（直接 ${esc(item.summary.direct_count ?? 0)}）`],
        ['可重算 / 隔离', `${esc(item.summary.refreshable_count ?? 0)} / <span class="badge ${isolated.length ? 'danger' : 'ok'}">${esc(item.summary.isolated_count ?? 0)}</span>`],
        ['波次', `${esc(item.summary.wave_count ?? waves.length)} 波`],
        ['下游报表', `${esc(item.summary.report_count ?? 0)} 张（核心 ${esc(item.summary.core_report_count ?? 0)} 张）`],
        ['开始 / 结束', `${fmtTime(item.started_at)} → ${fmtTime(item.finished_at)}`],
      ])}
      <div class="dsl-box">
        <span class="label">刷新日志</span>
        <p class="section-note">${esc(item.log_text || '—')}</p>
      </div>
      <div class="dsl-box">
        <span class="label">波次执行（按依赖顺序，已完成的波次不会重复执行）</span>
        <div class="list-tight">
          ${
            waves.length
              ? waves
                  .map(
                    (wave) => `<div class="impact-row" style="align-items:flex-start">
                      <div style="display:grid;gap:4px">
                        <span class="index-tag">WAVE ${String(wave.index + 1).padStart(2, '0')} · ${wave.metric_codes.length} 个指标</span>
                        <span class="mono tiny">${esc(wave.metric_codes.join(' / '))}</span>
                        <span class="muted tiny">${esc(wave.explanation)}</span>
                        ${wave.error_text ? `<span class="tiny" style="color:var(--danger)">${esc(wave.error_text)}</span>` : ''}
                      </div>
                      <div>${waveStatusBadge(wave.status)}</div>
                    </div>`,
                  )
                  .join('')
              : '<p class="muted">没有可执行波次：整个下游闭包都被隔离。</p>'
          }
        </div>
      </div>
      <div class="dsl-box">
        <span class="label">隔离分支（只停受影响的下游，不影响其他波次）</span>
        <div class="list-tight">
          ${
            isolated.length
              ? isolated
                  .map(
                    (entry) => `<div class="impact-row" style="align-items:flex-start">
                      <div style="display:grid;gap:4px">
                        <span><span class="index-tag">${esc(entry.code)}</span> <span class="badge danger">${esc(ENUMS.isolateReason[entry.reason] || entry.reason)}</span></span>
                        <span class="muted tiny">${esc(entry.explanation)}</span>
                      </div>
                    </div>`,
                  )
                  .join('')
              : '<p class="muted">没有隔离分支。</p>'
          }
        </div>
      </div>
      <div class="dsl-box">
        <span class="label">影响面 · 闭包内指标的下游报表</span>
        ${impactRows(item.reports || {})}
      </div>`,
  });
}

async function renderDetail(view, ctx, id) {
  const item = await api.get(`/api/refreshes/${id}`);
  openBatch(view, ctx, item);
}

export const refreshesPage = {
  async render(view, ctx) {
    let data;
    let options;
    try {
      data = await api.get('/api/refreshes', { status: state.status, limit: 50 });
      options = can(ctx.user.role, 'refresh:create') ? await api.get('/api/refreshes/options') : { fields: [], metrics: [] };
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => refreshesPage.render(view, ctx));
      return;
    }
    const selected = state.kind === 'field' ? options.fields : options.metrics;
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>月结刷新</h2>
          <span class="meta">${data.total} 批 · 同一批变更只保留一条记录</span>
        </div>
        <div class="panel-body">
          ${
            can(ctx.user.role, 'refresh:create')
              ? `<div class="filter-bar" id="refresh-form">
                  ${field({ label: '周期', name: 'period', value: state.period, placeholder: 'YYYY-MM（默认当前月）' })}
                  ${field({ label: '变更点类型', name: 'kind', value: state.kind, options: Object.entries(ENUMS.changePointKind).map(([value, label]) => ({ value, label })) })}
                  ${field({ label: '变更点', name: 'ref', value: state.ref, options: [{ value: '', label: '选择变更点' }, ...selected.map((item) => ({ value: item.ref, label: `${item.ref} · 下游 ${item.closure_count}` }))] })}
                  <div class="spacer"></div>
                  <button class="btn sm ghost" id="preview-refresh" type="button">预览影响</button>
                  <button class="btn sm accent" id="run-refresh" type="button">执行刷新</button>
                </div>
                <div id="preview-box"></div>`
              : '<p class="section-note">只读角色可查看刷新记录与隔离分支，执行刷新需要专员及以上角色。</p>'
          }
        </div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>刷新记录</h2><span class="meta">${data.total} 条</span></div>
        ${filterBar('refresh-status', [
          { label: '状态', name: 'status', value: state.status, options: [{ value: '', label: '全部状态' }, ...Object.entries(ENUMS.refreshStatus).map(([value, label]) => ({ value, label }))] },
        ])}
        ${table({
          rows: data.items,
          columns: [
            { key: 'refresh_no', label: '刷新号', className: 'mono' },
            { key: 'period', label: '周期', className: 'mono' },
            {
              key: 'change_points',
              label: '变更点',
              render: (row) => esc((row.change_points || []).map((p) => p.ref).join(' / ')),
            },
            { key: 'summary', label: '下游闭包', className: 'num', render: (row) => esc(row.summary.closure_count ?? 0) },
            { key: 'summary', label: '波次', className: 'num', render: (row) => esc(row.summary.wave_count ?? (row.waves || []).length) },
            {
              key: 'summary',
              label: '隔离',
              className: 'num',
              render: (row) => `<span class="badge ${row.summary.isolated_count ? 'danger' : 'ok'}">${esc(row.summary.isolated_count ?? 0)}</span>`,
            },
            {
              key: 'status',
              label: '状态',
              render: (row) => `<span class="badge ${toneFor(row.status)}">${esc(ENUMS.refreshStatus[row.status] || row.status)}</span>`,
            },
            { key: 'started_at', label: '开始', render: (row) => `<span class="tiny">${fmtTime(row.started_at)}</span>` },
          ],
          empty: { title: '还没有月结刷新', hint: '选一个上游字段或基础指标作为变更点，先预览影响再执行。' },
        })}
      </section>`;

    const form = view.querySelector('#refresh-form');
    const previewBox = view.querySelector('#preview-box');
    const payload = () => ({
      period: form.querySelector('[name=period]').value.trim() || null,
      change_points: [{ kind: state.kind, ref: state.ref }],
    });
    const renderPreview = (plan) => {
      previewBox.innerHTML = `
        <div class="dsl-box">
          <span class="label">影响预览 · ${esc(plan.summary.closure_count)} 个指标 / ${esc(plan.summary.wave_count)} 波 / 隔离 ${esc(plan.summary.isolated_count)}</span>
          <div class="list-tight">
            ${plan.waves
              .map(
                (wave) => `<div class="impact-row" style="align-items:flex-start">
                  <div style="display:grid;gap:4px">
                    <span class="index-tag">WAVE ${String(wave.index + 1).padStart(2, '0')}</span>
                    <span class="mono tiny">${esc(wave.metric_codes.join(' / '))}</span>
                    <span class="muted tiny">${esc(wave.explanation)}</span>
                  </div>
                </div>`,
              )
              .join('') || '<p class="muted tiny">闭包全部被隔离，没有可执行波次。</p>'}
            ${plan.isolated
              .map(
                (entry) => `<div class="impact-row"><span><span class="index-tag">${esc(entry.code)}</span> <span class="badge danger">${esc(ENUMS.isolateReason[entry.reason] || entry.reason)}</span></span><span class="muted tiny">${esc(entry.explanation)}</span></div>`,
              )
              .join('')}
          </div>
        </div>`;
    };

    form?.addEventListener('change', async (event) => {
      if (event.target.name === 'kind') {
        state.kind = event.target.value;
        state.ref = '';
        await refreshesPage.render(view, ctx);
        return;
      }
      state.ref = form.querySelector('[name=ref]').value;
      state.period = form.querySelector('[name=period]').value.trim();
    });
    view.querySelector('#preview-refresh')?.addEventListener('click', async () => {
      if (!state.ref) return toast('先选择一个变更点', 'err');
      try {
        renderPreview(await api.post('/api/refreshes/preview', payload()));
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
    view.querySelector('#run-refresh')?.addEventListener('click', async () => {
      if (!state.ref) return toast('先选择一个变更点', 'err');
      try {
        const result = await api.post('/api/refreshes', payload());
        toast(
          result.reused
            ? `同一批变更已刷新过：${result.refresh.refresh_no}（未新增记录）`
            : `刷新完成：${result.refresh.refresh_no}，隔离 ${result.refresh.summary.isolated_count ?? 0} 个`,
        );
        await refreshesPage.render(view, ctx);
        await renderDetail(view, ctx, result.refresh.id);
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const id = row.dataset.id;
        if (id) renderDetail(view, ctx, id);
      });
      row.style.cursor = 'pointer';
    });
    view.querySelector('#refresh-status')?.addEventListener('change', (event) => {
      state.status = event.target.value;
      refreshesPage.render(view, ctx);
    });  },
};

export default refreshesPage;
