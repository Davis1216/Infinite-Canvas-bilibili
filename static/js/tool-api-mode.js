(function(){
    const DEFAULT_IMAGE_MODELS = ['gpt-image-2', 'gpt-image-2-2k', 'gpt-image-2-4k', 'nano-banana'];
    const stateMap = new WeakMap();

    function injectStyles(){
        if(document.getElementById('studioToolApiStyles')) return;
        const style = document.createElement('style');
        style.id = 'studioToolApiStyles';
        style.textContent = `
            .tool-api-panel{background:#fff;border:1px solid #e5e7eb;border-radius:18px;padding:14px;display:flex;flex-direction:column;gap:12px;box-shadow:0 12px 24px rgba(15,23,42,.04)}
            .tool-api-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);gap:10px}
            .tool-api-field{display:flex;flex-direction:column;gap:6px;min-width:0}
            .tool-api-label{font-size:9px;font-weight:900;color:#94a3b8;text-transform:uppercase;letter-spacing:.16em;display:flex;align-items:center;gap:5px}
            .tool-api-select,.tool-api-number{height:34px;border:1px solid #e5e7eb;border-radius:12px;background:#f8fafc;color:#111827;font-size:11px;font-weight:800;padding:0 10px;outline:none;min-width:0}
            .tool-api-select:focus,.tool-api-number:focus{border-color:#111827;background:#fff;box-shadow:0 0 0 1px #111827}
            .tool-api-temp{display:grid;grid-template-columns:auto minmax(90px,1fr) 56px;align-items:center;gap:10px}
            .tool-api-temp input[type=range]{width:100%;accent-color:#111827}
            .tool-api-empty{font-size:10px;line-height:1.6;color:#64748b;background:#f8fafc;border:1px dashed #cbd5e1;border-radius:14px;padding:10px 12px}
            @media (max-width:640px){.tool-api-grid{grid-template-columns:1fr}.tool-api-temp{grid-template-columns:1fr 56px}.tool-api-temp .tool-api-label{grid-column:1/-1}}
        `;
        document.head.appendChild(style);
    }

    function normalizeModelList(values){
        if(!Array.isArray(values)) return [];
        const seen = new Set();
        const out = [];
        values.forEach(item => {
            const value = typeof item === 'string' ? item : (item && (item.id || item.model || item.name || item.value));
            const text = String(value || '').trim();
            if(text && !seen.has(text)){
                seen.add(text);
                out.push(text);
            }
        });
        return out;
    }

    function runninghubEntries(provider, key, prefix){
        return (Array.isArray(provider?.[key]) ? provider[key] : [])
            .filter(item => item && item.enabled !== false && item.hidden !== true)
            .map(item => {
                const id = String(item.workflowId || item.webappId || item.id || '').trim();
                if(!id) return null;
                const label = String(item.name || item.title || id).trim();
                return { value:`${prefix}:${id}`, label };
            })
            .filter(Boolean);
    }

    function providerModels(provider, cfg){
        if(!provider) return [];
        if(provider.id === 'runninghub' || provider.protocol === 'runninghub'){
            const entries = [
                ...runninghubEntries(provider, 'rh_workflows', 'workflow'),
                ...runninghubEntries(provider, 'rh_apps', 'app')
            ];
            if(entries.length) return entries;
        }
        const models = normalizeModelList(provider.image_models);
        return models.map(model => ({ value:model, label:model }));
    }

    function imageProviders(cfg){
        const providers = Array.isArray(cfg?.api_providers) ? cfg.api_providers : [];
        return providers.filter(provider => {
            if(!provider || provider.enabled === false) return false;
            if(provider.id === 'modelscope') return false;
            if(provider.id === 'comfy' || provider.id === 'comfly') return false;
            return providerModels(provider, cfg).length > 0;
        });
    }

    async function loadConfig(){
        const res = await fetch('/api/config');
        if(!res.ok) throw new Error(`Config load failed: ${res.status}`);
        return await res.json();
    }

    function clampTemperature(value){
        const n = Number.parseFloat(value);
        if(!Number.isFinite(n)) return 0.7;
        return Math.max(0, Math.min(2, Math.round(n * 10) / 10));
    }

    function syncTemperature(state, value){
        state.temperature = clampTemperature(value);
        if(state.range && document.activeElement !== state.range) state.range.value = String(state.temperature);
        if(state.number && document.activeElement !== state.number) state.number.value = state.temperature.toFixed(1);
    }

    function renderOptions(select, options, selected){
        select.innerHTML = options.map(option => {
            const value = String(option.value || '');
            const label = String(option.label || option.value || '');
            const isSelected = value === selected ? ' selected' : '';
            return `<option value="${value.replace(/"/g, '&quot;')}"${isSelected}>${label}</option>`;
        }).join('');
    }

    function refreshModelOptions(state){
        const provider = state.providers.find(item => item.id === state.providerSelect.value) || state.providers[0];
        const options = providerModels(provider, state.config);
        renderOptions(state.modelSelect, options, state.modelSelect.value);
    }

    async function initControls(target, options){
        injectStyles();
        const el = typeof target === 'string' ? document.getElementById(target) : target;
        if(!el) throw new Error('API controls target not found');
        const state = {
            config:null,
            providers:[],
            providerSelect:null,
            modelSelect:null,
            range:null,
            number:null,
            temperature:clampTemperature(options?.temperature ?? 0.7)
        };
        state.config = await loadConfig();
        state.providers = imageProviders(state.config);
        if(!state.providers.length){
            el.innerHTML = `<div class="tool-api-empty">No enabled custom image API provider is available. Add one in API settings first.</div>`;
            stateMap.set(el, state);
            return state;
        }
        el.innerHTML = `
            <div class="tool-api-panel">
                <div class="tool-api-grid">
                    <label class="tool-api-field">
                        <span class="tool-api-label"><i data-lucide="plug" class="w-3 h-3"></i>Provider</span>
                        <select class="tool-api-select" data-tool-api-provider></select>
                    </label>
                    <label class="tool-api-field">
                        <span class="tool-api-label"><i data-lucide="box" class="w-3 h-3"></i>Model</span>
                        <select class="tool-api-select" data-tool-api-model></select>
                    </label>
                </div>
                <label class="tool-api-temp">
                    <span class="tool-api-label"><i data-lucide="thermometer" class="w-3 h-3"></i>Temperature</span>
                    <input type="range" min="0" max="2" step="0.1" value="${state.temperature}">
                    <input class="tool-api-number" type="number" min="0" max="2" step="0.1" value="${state.temperature.toFixed(1)}">
                </label>
            </div>
        `;
        state.providerSelect = el.querySelector('[data-tool-api-provider]');
        state.modelSelect = el.querySelector('[data-tool-api-model]');
        state.range = el.querySelector('input[type=range]');
        state.number = el.querySelector('input[type=number]');
        renderOptions(state.providerSelect, state.providers.map(provider => ({value:provider.id, label:provider.name || provider.id})));
        refreshModelOptions(state);
        state.providerSelect.addEventListener('change', () => refreshModelOptions(state));
        state.range.addEventListener('input', () => syncTemperature(state, state.range.value));
        state.number.addEventListener('input', () => syncTemperature(state, state.number.value));
        stateMap.set(el, state);
        if(window.lucide) lucide.createIcons();
        return state;
    }

    function getState(target){
        if(target?.providerSelect) return target;
        const el = typeof target === 'string' ? document.getElementById(target) : target;
        return stateMap.get(el);
    }

    function selection(target){
        const state = getState(target);
        if(!state?.providerSelect || !state?.modelSelect) throw new Error('Custom API controls are not ready');
        return {
            provider_id: state.providerSelect.value,
            model: state.modelSelect.value,
            temperature: clampTemperature(state.temperature)
        };
    }

    async function uploadReference(file){
        const form = new FormData();
        form.append('files', file);
        const res = await fetch('/api/ai/upload', { method:'POST', body:form });
        const data = await res.json().catch(() => ({}));
        if(!res.ok || !data.files?.[0]) throw new Error(data.detail || 'Reference upload failed');
        return data.files[0];
    }

    async function generateImage(payload){
        const body = {
            prompt: payload.prompt || '',
            provider_id: payload.provider_id,
            model: payload.model,
            size: payload.size || '1024x1024',
            quality: payload.quality || 'auto',
            n: payload.n || 1,
            temperature: clampTemperature(payload.temperature),
            reference_images: Array.isArray(payload.reference_images) ? payload.reference_images : []
        };
        const res = await fetch('/api/online-image', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify(body)
        });
        const data = await res.json().catch(() => ({}));
        if(!res.ok || data.error){
            const detail = data.detail || data.message || data.error || `HTTP ${res.status}`;
            throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
        }
        return data;
    }

    window.StudioToolApi = {
        initControls,
        selection,
        uploadReference,
        generateImage,
        clampTemperature
    };
})();
