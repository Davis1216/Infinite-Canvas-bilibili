(function(){
    const state = {
        summary:null,
        pricing:null,
        sort:{ key:'cost', dir:'desc' },
        debounce:null,
    };
    const colors = ['#2563eb','#059669','#d97706','#7c3aed','#dc2626','#0891b2','#4f46e5','#be123c','#0f766e','#9333ea','#ca8a04','#475569'];
    const $ = id => document.getElementById(id);
    const fmt = new Intl.NumberFormat('zh-CN');

    function escapeHtml(value){
        return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }

    function money(value, currency){
        const amount = Number(value || 0);
        const symbol = currency === 'USD' ? '$' : '¥';
        const digits = amount >= 100 ? 1 : amount >= 1 ? 3 : amount >= 0.01 ? 4 : 6;
        return `${symbol}${amount.toFixed(digits)}`;
    }

    function compact(value){
        const num = Number(value || 0);
        if(num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
        if(num >= 1000) return `${(num / 1000).toFixed(1)}K`;
        return fmt.format(num);
    }

    function normalizeModel(model){
        return String(model || 'unknown').trim().replace(/\\/g,'/').replace(/\s+/g,'').replace(/^(models\/|model\/)/i,'').toLowerCase().slice(0,180) || 'unknown';
    }

    function modelKey(providerId, model){
        return `${String(providerId || 'unknown').toLowerCase()}::${normalizeModel(model)}`;
    }

    function defaultDates(){
        $('startInput').value = '';
        $('endInput').value = '';
    }

    function queryString(){
        const params = new URLSearchParams();
        const pairs = [
            ['start', $('startInput').value],
            ['end', $('endInput').value],
            ['provider', $('providerSelect').value],
            ['model', $('modelSelect').value],
            ['kind', $('kindSelect').value],
        ];
        pairs.forEach(([key,value]) => { if(value) params.set(key, value); });
        if($('billableOnlyInput').checked) params.set('billable_only', 'true');
        return params.toString();
    }

    async function fetchJson(url, options){
        const res = await fetch(url, options);
        if(!res.ok) throw new Error(await res.text());
        return res.json();
    }

    async function loadPricing(){
        state.pricing = await fetchJson('/api/usage/pricing');
        $('currencySelect').value = state.pricing.currency || 'CNY';
    }

    async function loadSummary(){
        const qs = queryString();
        state.summary = await fetchJson(`/api/usage/summary${qs ? `?${qs}` : ''}`);
        renderSummary();
    }

    async function refresh(){
        try {
            $('refreshBtn')?.classList.add('is-loading');
            if(!state.pricing) await loadPricing();
            await loadSummary();
            renderPricing();
        } catch(err) {
            console.error(err);
            showEmpty(`加载失败：${String(err.message || err).slice(0,160)}`);
        } finally {
            $('refreshBtn')?.classList.remove('is-loading');
            if(window.lucide) lucide.createIcons();
        }
    }

    function scheduleRefresh(){
        clearTimeout(state.debounce);
        state.debounce = setTimeout(refresh, 220);
    }

    function showEmpty(message){
        $('emptyState').hidden = false;
        $('emptyState').querySelector('span').textContent = message || '调整筛选条件，或先进行一次 GPT 对话/在线生图。';
    }

    function renderSummary(){
        const data = state.summary;
        const currency = data.currency || 'CNY';
        const kpi = data.kpis || {};
        $('totalCost').textContent = money(kpi.total_cost, currency);
        $('monthCost').textContent = `本月 ${money(kpi.month_cost, currency)} · ${formatDelta(kpi.month_cost_delta)}`;
        $('requestCount').textContent = compact(kpi.request_count);
        $('tokenCount').textContent = compact(kpi.total_tokens);
        $('imageCount').textContent = compact(kpi.image_count);
        $('unpricedHint').textContent = `输出图 ${compact(kpi.image_output_count || 0)} 张 · 未配置价格 ${compact(data.unpriced_count || 0)} 条`;
        renderOptions('providerSelect', data.providers || [], '全部供应商');
        renderOptions('modelSelect', data.models || [], '全部模型');
        $('emptyState').hidden = Boolean(data.event_count);
        if(!data.event_count) showEmpty();
        $('trendMeta').textContent = `${compact(data.event_count || 0)} 条事件`;
        drawTrend(data.trend || [], currency);
        drawPie('providerPie', 'providerLegend', data.provider_pie || [], currency);
        drawPie('modelPie', 'modelLegend', data.model_pie || [], currency);
        renderTopModels(data.top_models || [], currency);
        renderTable();
    }

    function formatDelta(delta){
        const value = Number(delta || 0);
        if(!Number.isFinite(value) || value === 0) return '环比 0%';
        return `环比 ${value > 0 ? '+' : ''}${(value * 100).toFixed(1)}%`;
    }

    function renderOptions(selectId, items, placeholder){
        const select = $(selectId);
        const previous = select.value;
        const html = [`<option value="">${escapeHtml(placeholder)}</option>`]
            .concat(items.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`))
            .join('');
        select.innerHTML = html;
        if([...select.options].some(opt => opt.value === previous)) select.value = previous;
    }

    function scaledPoints(rows, field, width, height, pad){
        const max = Math.max(1, ...rows.map(row => Number(row[field] || 0)));
        return rows.map((row, index) => {
            const x = rows.length <= 1 ? pad.left : pad.left + index * ((width - pad.left - pad.right) / (rows.length - 1));
            const y = height - pad.bottom - (Number(row[field] || 0) / max) * (height - pad.top - pad.bottom);
            return [x,y];
        });
    }

    function pathFromPoints(points){
        return points.map((point, index) => `${index ? 'L' : 'M'}${point[0].toFixed(1)} ${point[1].toFixed(1)}`).join(' ');
    }

    function svgLocalPoint(svg, event){
        const point = svg.createSVGPoint();
        point.x = event.clientX;
        point.y = event.clientY;
        return point.matrixTransform(svg.getScreenCTM().inverse());
    }

    function positionTooltip(host, tooltip, clientX, clientY, html){
        if(!tooltip) return;
        tooltip.innerHTML = html;
        tooltip.hidden = false;
        tooltip.style.left = `${Math.max(86, Math.min(window.innerWidth - 86, clientX))}px`;
        tooltip.style.top = `${Math.max(68, clientY)}px`;
    }

    function hideTooltip(){
        const tooltip = $('chartTooltip');
        if(tooltip) tooltip.hidden = true;
    }

    function animateSvgLines(svg){
        requestAnimationFrame(() => {
            svg.querySelectorAll('.metric-line').forEach((path, index) => {
                const len = Math.ceil(path.getTotalLength?.() || 0);
                if(!len) return;
                path.style.setProperty('--line-length', len);
                path.style.strokeDasharray = String(len);
                path.style.strokeDashoffset = String(len);
                path.style.animation = 'none';
                path.getBoundingClientRect();
                path.style.animation = `usage-line-draw .72s var(--ease) both ${index * 70}ms`;
            });
        });
    }

    function setTrendMetricFocus(svg, legend, metricKey){
        svg.classList.toggle('has-focus', Boolean(metricKey));
        legend?.classList.toggle('has-focus', Boolean(metricKey));
        svg.querySelectorAll('.metric-line').forEach(el => el.classList.toggle('is-active', el.dataset.metric === metricKey));
        legend?.querySelectorAll('.trend-legend-item').forEach(el => el.classList.toggle('is-active', el.dataset.metric === metricKey));
    }

    function bindTrendInteractions(svg, legend, rows, metrics, pointsByMetric, pad, width, height, currency){
        const hoverLine = svg.querySelector('.hover-line');
        const tooltip = $('chartTooltip');
        const host = svg.closest('.chart-panel') || svg.parentElement;
        const plotLeft = pad.left;
        const plotRight = width - pad.right;
        const step = rows.length <= 1 ? 1 : (plotRight - plotLeft) / (rows.length - 1);
        legend?.querySelectorAll('.trend-legend-item').forEach(item => {
            item.onmouseenter = () => setTrendMetricFocus(svg, legend, item.dataset.metric);
            item.onmouseleave = () => setTrendMetricFocus(svg, legend, '');
        });
        svg.onmouseleave = () => {
            svg.classList.remove('has-hover');
            setTrendMetricFocus(svg, legend, '');
            if(hoverLine) hoverLine.setAttribute('visibility', 'hidden');
            svg.querySelectorAll('.metric-dot').forEach(dot => dot.classList.remove('is-nearest'));
            hideTooltip();
        };
        svg.onmousemove = event => {
            const p = svgLocalPoint(svg, event);
            if(p.x < plotLeft || p.x > plotRight || p.y < pad.top - 12 || p.y > height - pad.bottom + 12){
                svg.onmouseleave();
                return;
            }
            const rowIndex = Math.max(0, Math.min(rows.length - 1, Math.round((p.x - plotLeft) / step)));
            const row = rows[rowIndex];
            const x = rows.length <= 1 ? plotLeft : plotLeft + rowIndex * step;
            svg.classList.add('has-hover');
            if(hoverLine){
                hoverLine.setAttribute('x1', x);
                hoverLine.setAttribute('x2', x);
                hoverLine.setAttribute('visibility', 'visible');
            }
            const nearestMetric = metrics
                .map(metric => {
                    const pt = pointsByMetric[metric.field]?.[rowIndex];
                    return pt ? {metric, dist:Math.abs(pt[1] - p.y)} : null;
                })
                .filter(Boolean)
                .sort((a,b) => a.dist - b.dist)[0]?.metric;
            setTrendMetricFocus(svg, legend, nearestMetric?.field || '');
            svg.querySelectorAll('.metric-dot').forEach(dot => dot.classList.toggle('is-nearest', Number(dot.dataset.row) === rowIndex));
            const detailRows = metrics.map(metric => `
                <div><span>${escapeHtml(metric.label)}</span><b>${escapeHtml(metric.value(row))}</b></div>
            `).join('');
            positionTooltip(host, tooltip, event.clientX, event.clientY, `<strong>${escapeHtml(row.date || '')}</strong>${detailRows}`);
        };
    }

    function setPieFocus(svg, legend, index){
        const active = index !== '' && index != null;
        svg.classList.toggle('has-focus', active);
        legend?.classList.toggle('has-focus', active);
        svg.querySelectorAll('.pie-slice').forEach(el => el.classList.toggle('is-active', el.dataset.index === String(index)));
        legend?.querySelectorAll('.legend-row').forEach(el => el.classList.toggle('is-active', el.dataset.index === String(index)));
    }

    function bindPieInteractions(svg, legend, rows, total, currency){
        const host = svg.closest('.panel') || svg.parentElement;
        const tooltip = $('chartTooltip');
        const leave = () => {
            setPieFocus(svg, legend, '');
            hideTooltip();
        };
        svg.onmouseleave = leave;
        legend.onmouseleave = leave;
        const show = (index, event) => {
            const row = rows[index];
            if(!row) return;
            setPieFocus(svg, legend, index);
            const pct = ((Number(row.value || 0) / total) * 100).toFixed(1);
            const value = row.is_request_fallback ? `${compact(row.value)} 次` : money(row.value, currency);
            positionTooltip(host, tooltip, event.clientX, event.clientY, `
                <strong>${escapeHtml(row.label)}</strong>
                <div><span>占比</span><b>${pct}%</b></div>
                <div><span>${row.is_request_fallback ? '请求' : '费用'}</span><b>${escapeHtml(value)}</b></div>
            `);
        };
        svg.querySelectorAll('.pie-slice').forEach(slice => {
            slice.onmouseenter = event => show(Number(slice.dataset.index), event);
            slice.onmousemove = event => show(Number(slice.dataset.index), event);
            slice.onfocus = event => show(Number(slice.dataset.index), event);
            slice.onblur = leave;
        });
        legend.querySelectorAll('.legend-row').forEach(row => {
            row.onmouseenter = event => show(Number(row.dataset.index), event);
            row.onmousemove = event => show(Number(row.dataset.index), event);
        });
    }

    function drawTrend(rows, currency){
        const svg = $('trendChart');
        const legend = $('trendLegend');
        const width = 760, height = 260;
        const pad = {left:44,right:18,top:18,bottom:34};
        if(!rows.length){
            svg.innerHTML = `<text x="380" y="132" text-anchor="middle">暂无趋势数据</text>`;
            if(legend) legend.innerHTML = '';
            return;
        }
        const metrics = [
            {field:'cost', label:'费用', className:'cost-line', color:'var(--usage-blue)', value:row => money(row.cost, currency)},
            {field:'requests', label:'请求', className:'request-line', color:'var(--usage-green)', value:row => `${compact(row.requests)} 次`},
            {field:'tokens', label:'GPT Token', className:'token-line', color:'var(--usage-amber)', value:row => `${compact(row.tokens)} token`},
            {field:'images', label:'生图次数', className:'image-line', color:'#e11d48', value:row => `${compact(row.images)} 次`},
        ].map(metric => {
            const values = rows.map(row => Number(row[metric.field] || 0));
            const activeRows = rows.filter(row => Number(row[metric.field] || 0) > 0);
            return {...metric, max:Math.max(0, ...values), activeRows};
        }).filter(metric => metric.max > 0);
        if(legend){
            legend.innerHTML = metrics.map(metric => `
                <span class="trend-legend-item" data-metric="${escapeHtml(metric.field)}">
                    <i style="background:${metric.color}"></i>
                    <b>${escapeHtml(metric.label)}</b>
                    <small>峰值 ${escapeHtml(metric.value({[metric.field]:metric.max}))}</small>
                </span>
            `).join('') || '<span class="trend-legend-item">暂无有效指标</span>';
        }
        const grid = [0,1,2,3].map(i => {
            const y = pad.top + i * ((height - pad.top - pad.bottom) / 3);
            return `<line class="grid-line" x1="${pad.left}" y1="${y}" x2="${width - pad.right}" y2="${y}"></line>`;
        }).join('');
        const labels = rows.filter((_,i) => rows.length < 8 || i % Math.ceil(rows.length / 6) === 0 || i === rows.length - 1)
            .map((row,i,arr) => {
                const index = rows.indexOf(row);
                const x = rows.length <= 1 ? pad.left : pad.left + index * ((width - pad.left - pad.right) / (rows.length - 1));
                return `<text x="${x}" y="${height - 10}" text-anchor="${i === 0 ? 'start' : i === arr.length - 1 ? 'end' : 'middle'}">${escapeHtml(String(row.date || '').slice(5))}</text>`;
            }).join('');
        const pointsByMetric = {};
        const metricShapes = metrics.map((metric, metricIndex) => {
            const pts = scaledPoints(rows, metric.field, width, height, pad);
            pointsByMetric[metric.field] = pts;
            const activeIndexes = rows.map((row,index) => Number(row[metric.field] || 0) > 0 ? index : -1).filter(index => index >= 0);
            const dots = activeIndexes.map(index => `<circle class="metric-dot ${metric.className}-dot" data-metric="${escapeHtml(metric.field)}" data-row="${index}" cx="${pts[index][0].toFixed(1)}" cy="${pts[index][1].toFixed(1)}" r="4"><title>${escapeHtml(rows[index].date)} · ${escapeHtml(metric.label)} ${escapeHtml(metric.value(rows[index]))}</title></circle>`).join('');
            if(activeIndexes.length <= 1){
                return dots;
            }
            return `<path class="metric-line ${metric.className}" data-metric="${escapeHtml(metric.field)}" style="animation-delay:${metricIndex * 70}ms" d="${pathFromPoints(pts)}"></path>${dots}`;
        }).join('');
        svg.innerHTML = `
            ${grid}
            ${metricShapes}
            ${labels}
            <line class="hover-line" x1="${pad.left}" x2="${pad.left}" y1="${pad.top}" y2="${height - pad.bottom}" visibility="hidden"></line>
            <rect class="hover-band" x="${pad.left}" y="${pad.top - 12}" width="${width - pad.left - pad.right}" height="${height - pad.top - pad.bottom + 24}"></rect>
            <text x="${pad.left}" y="12">各指标独立归一化，仅看各自走势</text>
        `;
        animateSvgLines(svg);
        bindTrendInteractions(svg, legend, rows, metrics, pointsByMetric, pad, width, height, currency);
    }

    function drawPie(svgId, legendId, rows, currency){
        const svg = $(svgId), legend = $(legendId);
        const total = rows.reduce((sum,row) => sum + Number(row.value || 0), 0);
        if(!rows.length || total <= 0){
            svg.innerHTML = `<circle cx="110" cy="110" r="82" fill="var(--usage-soft)" stroke="var(--usage-line)" stroke-width="1"></circle><text x="110" y="115" text-anchor="middle" fill="currentColor">暂无</text>`;
            legend.innerHTML = '';
            return;
        }
        let angle = -Math.PI / 2;
        const cx = 110, cy = 110, radius = 82;
        const slicePath = (start, end) => {
            const x1 = cx + radius * Math.cos(start);
            const y1 = cy + radius * Math.sin(start);
            const x2 = cx + radius * Math.cos(end);
            const y2 = cy + radius * Math.sin(end);
            const large = end - start > Math.PI ? 1 : 0;
            return `M ${cx} ${cy} L ${x1.toFixed(3)} ${y1.toFixed(3)} A ${radius} ${radius} 0 ${large} 1 ${x2.toFixed(3)} ${y2.toFixed(3)} Z`;
        };
        const slices = rows.map((row, index) => {
            const pct = (Number(row.value || 0) / total) * 100;
            const label = row.is_request_fallback ? `${compact(row.value)} 次` : money(row.value, currency);
            if(pct >= 99.999){
                return `<circle class="pie-slice" data-index="${index}" tabindex="0" cx="${cx}" cy="${cy}" r="${radius}" fill="${colors[index % colors.length]}" style="animation-delay:${index * 45}ms"><title>${escapeHtml(row.label)} ${pct.toFixed(1)}% · ${label}</title></circle>`;
            }
            const slice = (Number(row.value || 0) / total) * Math.PI * 2;
            const path = slicePath(angle, angle + slice);
            angle += slice;
            return `<path class="pie-slice" data-index="${index}" tabindex="0" d="${path}" fill="${colors[index % colors.length]}" style="animation-delay:${index * 45}ms"><title>${escapeHtml(row.label)} ${pct.toFixed(1)}% · ${label}</title></path>`;
        }).join('');
        const leadPct = ((Number(rows[0]?.value || 0) / total) * 100).toFixed(1);
        svg.innerHTML = `
            <circle cx="${cx}" cy="${cy}" r="${radius}" fill="var(--usage-soft)" stroke="var(--usage-line)" stroke-width="1"></circle>
            ${slices}
            <circle cx="${cx}" cy="${cy}" r="${radius}" fill="none" stroke="var(--usage-line)" stroke-width="1"></circle>
            <text x="${cx}" y="${cy + radius + 22}" text-anchor="middle" fill="var(--usage-muted)" font-size="10" font-weight="850">Top ${leadPct}%</text>
        `;
        legend.innerHTML = rows.slice(0,8).map((row,index) => `
            <div class="legend-row" data-index="${index}">
                <span class="legend-dot" style="background:${colors[index % colors.length]}"></span>
                <span class="legend-label" title="${escapeHtml(row.label)}">${escapeHtml(row.label)}</span>
                <span>${((Number(row.value || 0) / total) * 100).toFixed(1)}% · ${row.is_request_fallback ? `${compact(row.value)}次` : money(row.value, currency)}</span>
            </div>
        `).join('');
        bindPieInteractions(svg, legend, rows, total, currency);
    }

    function renderTopModels(rows, currency){
        const max = Math.max(1, ...rows.map(row => Number(row.cost || row.request_count || 0)));
        $('topModels').innerHTML = rows.length ? rows.map((row,index) => {
            const value = Number(row.cost || row.request_count || 0);
            const pct = Math.max(3, Math.round((value / max) * 100));
            return `
                <div class="rank-row">
                    <div>
                        <div class="rank-title" title="${escapeHtml(row.model)}">${index + 1}. ${escapeHtml(row.model)}</div>
                        <div class="rank-meta">${escapeHtml(row.provider_name)} · ${compact(row.request_count)} 请求</div>
                    </div>
                    <div class="rank-value">${money(row.cost, currency)}</div>
                    <div class="rank-bar"><span style="width:${pct}%"></span></div>
                </div>
            `;
        }).join('') : '<div class="rank-meta">暂无模型数据</div>';
    }

    function renderTable(){
        const data = state.summary || {};
        const currency = data.currency || 'CNY';
        const search = $('tableSearch').value.trim().toLowerCase();
        let rows = (data.model_rows || []).filter(row => {
            if(!search) return true;
            return `${row.model} ${row.provider_name}`.toLowerCase().includes(search);
        });
        const {key, dir} = state.sort;
        rows.sort((a,b) => {
            const av = a[key], bv = b[key];
            const diff = typeof av === 'number' || typeof bv === 'number'
                ? Number(av || 0) - Number(bv || 0)
                : String(av || '').localeCompare(String(bv || ''));
            return dir === 'asc' ? diff : -diff;
        });
        $('usageTableBody').innerHTML = rows.map(row => `
            <tr>
                <td class="model-cell">
                    <div class="model-name" title="${escapeHtml(row.model)}">${escapeHtml(row.model)}</div>
                    <div class="model-sub">${escapeHtml(row.normalized_model)} · ${escapeHtml((row.kinds || []).join('/'))}</div>
                </td>
                <td>${escapeHtml(row.provider_name)}</td>
                <td>${compact(row.request_count)}</td>
                <td>${compact(row.total_tokens)}</td>
                <td>${compact(row.image_count)}</td>
                <td>${compact(row.image_output_count)}</td>
                <td>${compact(row.video_seconds)}</td>
                <td class="${row.has_pricing ? '' : 'price-missing'}">${row.has_pricing ? money(row.cost, currency) : '未计价'}</td>
                <td>${escapeHtml(row.last_used_label || '-')}</td>
            </tr>
        `).join('') || '<tr><td colspan="9">暂无匹配数据</td></tr>';
    }

    function kindLabel(kind){
        return ({chat:'聊天', image:'生图', video:'视频', local:'本地'}[kind] || '其他');
    }

    function priceFieldDefs(kind, scope='model'){
        if(kind === 'chat') return [['input_per_1m','输入/1M'], ['output_per_1m','输出/1M'], ['request_each','每请求']];
        if(kind === 'image') return [['image_each','每张图']];
        if(kind === 'video') return [['video_second','每秒 /s']];
        if(scope === 'provider') return [['input_per_1m','输入/1M'], ['output_per_1m','输出/1M'], ['image_each','每张图'], ['video_second','每秒 /s'], ['request_each','每请求']];
        return [['input_per_1m','输入/1M'], ['output_per_1m','输出/1M'], ['image_each','每张图'], ['video_second','每秒 /s'], ['request_each','每请求']];
    }

    function priceFields(value, kind, scope){
        const labels = priceFieldDefs(kind, scope);
        return labels.map(([key,label]) => `
            <label><span>${label}</span><input data-price-key="${key}" type="number" min="0" step="0.0001" value="${Number(value?.[key] || 0)}"></label>
        `).join('');
    }

    function renderPricing(){
        const pricing = state.pricing || {provider_defaults:{},model_overrides:{}};
        const providers = state.summary?.providers || [];
        $('providerPricingList').innerHTML = providers.map(provider => {
            const value = pricing.provider_defaults?.[provider.id] || {};
            return `
                <div class="price-row" data-provider-price="${escapeHtml(provider.id)}">
                    <label class="row-title"><span>供应商</span><input value="${escapeHtml(provider.name)}" disabled></label>
                    ${priceFields(value, '', 'provider')}
                    <button class="icon-btn remove-price-btn" type="button" data-clear-provider="${escapeHtml(provider.id)}" title="清空"><i data-lucide="eraser"></i></button>
                </div>
            `;
        }).join('') || '<div class="drawer-note">暂无已使用供应商；产生一次 GPT 对话、生图或画布调用后这里会出现。</div>';
        const overrides = pricing.model_overrides || {};
        const usedModels = state.summary?.price_models || [];
        const usedRows = usedModels.map(model => modelPriceRow(model.model_key, overrides[model.model_key] || {}, {locked:false, model}));
        const extraRows = Object.keys(overrides)
            .filter(key => !usedModels.some(model => model.model_key === key))
            .map(key => modelPriceRow(key, overrides[key]));
        $('modelPricingList').innerHTML = usedRows.concat(extraRows).join('') || '<div class="drawer-note">暂无已使用模型；也可以点“添加模型”先配置所有 API 模型价格。</div>';
        if(window.lucide) lucide.createIcons();
    }

    function modelPriceRow(key='', value={}, rowOptions={}){
        const models = state.summary?.all_price_models || state.summary?.price_models || state.summary?.model_rows || [];
        const selectedModel = rowOptions.model || models.find(row => (row.model_key || modelKey(row.provider_id, row.model || row.name)) === key) || null;
        const selectedLabel = selectedModel
            ? (selectedModel.name && selectedModel.name.includes('·') ? selectedModel.name : `${selectedModel.provider_name || selectedModel.provider_id || 'unknown'} · ${selectedModel.model || selectedModel.name || selectedModel.id}`)
            : '';
        const modelOptions = ['<option value="">选择模型</option>'].concat(models.map(row => {
            const k = row.model_key || modelKey(row.provider_id, row.model || row.name);
            const label = row.name && row.name.includes('·') ? row.name : `${row.provider_name || row.provider_id || 'unknown'} · ${row.model || row.name || row.id}`;
            return `<option value="${escapeHtml(k)}"${k === key ? ' selected' : ''}>${escapeHtml(label)}</option>`;
        })).join('');
        const kind = selectedModel?.kind || rowOptions.kind || '';
        const fieldCount = priceFieldDefs(kind, 'model').length;
        const gridColumns = `minmax(300px,2fr) repeat(${fieldCount},minmax(96px,.65fr)) auto`;
        return `
            <div class="price-row model-price-row" data-model-price data-model-kind="${escapeHtml(kind)}" style="grid-template-columns:${gridColumns}">
                <label class="row-title"><span>模型 ${kind ? `<b class="price-kind-badge">${escapeHtml(kindLabel(kind))}</b>` : ''}</span>${
                    rowOptions.model
                        ? `<input value="${escapeHtml(selectedLabel)}" title="${escapeHtml(selectedLabel)}" disabled><input type="hidden" data-model-key value="${escapeHtml(key)}">`
                        : `<select data-model-key>${modelOptions}</select>`
                }</label>
                ${priceFields(value, kind, 'model')}
                <button class="icon-btn remove-price-btn" type="button" data-remove-model-price title="删除"><i data-lucide="trash-2"></i></button>
            </div>
        `;
    }

    function collectPriceRow(row){
        const value = {};
        row.querySelectorAll('[data-price-key]').forEach(input => {
            value[input.dataset.priceKey] = Number(input.value || 0);
        });
        return value;
    }

    function hasAnyPrice(value){
        return Object.values(value || {}).some(num => Number(num || 0) > 0);
    }

    async function savePricing(event){
        event.preventDefault();
        const payload = {
            currency:$('currencySelect').value || 'CNY',
            provider_defaults:{},
            model_overrides:{},
        };
        document.querySelectorAll('[data-provider-price]').forEach(row => {
            const value = collectPriceRow(row);
            if(hasAnyPrice(value)) payload.provider_defaults[row.dataset.providerPrice] = value;
        });
        document.querySelectorAll('[data-model-price]').forEach(row => {
            const key = row.querySelector('[data-model-key]')?.value;
            const value = collectPriceRow(row);
            if(key && hasAnyPrice(value)) payload.model_overrides[key] = value;
        });
        state.pricing = await fetchJson('/api/usage/pricing', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify(payload),
        });
        closePricing();
        await loadSummary();
        renderPricing();
    }

    function openPricing(){
        renderPricing();
        $('pricingDrawer').classList.add('open');
        $('pricingDrawer').setAttribute('aria-hidden','false');
    }

    function closePricing(){
        $('pricingDrawer').classList.remove('open');
        $('pricingDrawer').setAttribute('aria-hidden','true');
    }

    function bind(){
        ['startInput','endInput','providerSelect','modelSelect','kindSelect','billableOnlyInput'].forEach(id => {
            $(id).addEventListener('change', scheduleRefresh);
        });
        $('tableSearch').addEventListener('input', renderTable);
        $('refreshBtn').addEventListener('click', refresh);
        $('pricingOpenBtn').addEventListener('click', openPricing);
        document.querySelectorAll('[data-close-pricing]').forEach(el => el.addEventListener('click', closePricing));
        $('pricingForm').addEventListener('submit', savePricing);
        $('addModelPriceBtn').addEventListener('click', () => {
            $('modelPricingList').insertAdjacentHTML('beforeend', modelPriceRow());
            if(window.lucide) lucide.createIcons();
        });
        $('modelPricingList').addEventListener('change', event => {
            const select = event.target.closest('[data-model-key]');
            if(!select || select.tagName !== 'SELECT') return;
            const row = select.closest('[data-model-price]');
            row.outerHTML = modelPriceRow(select.value, collectPriceRow(row));
            if(window.lucide) lucide.createIcons();
        });
        document.addEventListener('click', event => {
            const clearProvider = event.target.closest('[data-clear-provider]');
            if(clearProvider){
                clearProvider.closest('.price-row').querySelectorAll('[data-price-key]').forEach(input => input.value = '0');
            }
            if(event.target.closest('[data-remove-model-price]')){
                event.target.closest('[data-model-price]')?.remove();
            }
        });
        document.querySelectorAll('th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const key = th.dataset.sort;
                state.sort = { key, dir: state.sort.key === key && state.sort.dir === 'desc' ? 'asc' : 'desc' };
                renderTable();
            });
        });
        window.addEventListener('message', event => {
            if(event.origin && event.origin !== location.origin) return;
            if(event.data?.type === 'providers-changed') refresh();
        });
    }

    document.addEventListener('DOMContentLoaded', async () => {
        defaultDates();
        bind();
        if(window.lucide) lucide.createIcons();
        await loadPricing();
        await refresh();
    }, {once:true});
})();
