(function(global){
    'use strict';

    const SAFE_PROTOCOLS = new Set(['http:', 'https:', 'mailto:']);
    const IMAGE_PROTOCOLS = new Set(['http:', 'https:', 'data:', 'blob:']);

    function safeUrl(value, image){
        const input=String(value||'').trim();
        if(!input) return '';
        if(input.startsWith('/') || input.startsWith('./') || input.startsWith('../') || input.startsWith('#')) return input;
        try{
            const url=new URL(input, global.location?.href || 'http://localhost/');
            return (image ? IMAGE_PROTOCOLS : SAFE_PROTOCOLS).has(url.protocol) ? input : '';
        }catch(e){ return ''; }
    }

    function appendText(parent,value){ parent.appendChild(document.createTextNode(String(value||''))); }

    function findClosing(text, marker, start){
        let cursor=start;
        while(cursor<text.length){
            const found=text.indexOf(marker,cursor);
            if(found<0) return -1;
            let slashes=0;
            for(let i=found-1;i>=0&&text[i]==='\\';i--) slashes++;
            if(slashes%2===0) return found;
            cursor=found+marker.length;
        }
        return -1;
    }

    function appendInline(parent,source,options={},depth=0){
        const text=String(source||'');
        if(depth>8){ appendText(parent,text); return; }
        let i=0;
        while(i<text.length){
            if(text[i]==='\\' && i+1<text.length && /[\\`*{}\[\]()#+.!_>~-]/.test(text[i+1])){
                appendText(parent,text[i+1]); i+=2; continue;
            }
            if(text[i]==='\n'){
                parent.appendChild(document.createElement('br')); i++; continue;
            }
            if(text[i]==='`'){
                const end=findClosing(text,'`',i+1);
                if(end>i+1){ const code=document.createElement('code'); code.textContent=text.slice(i+1,end); parent.appendChild(code); i=end+1; continue; }
            }
            const imageMatch=text.slice(i).match(/^!\[([^\]]*)\]\((\S+?)(?:\s+["']([^"']*)["'])?\)/);
            if(imageMatch){
                const url=safeUrl(imageMatch[2],true);
                if(url){
                    const image=document.createElement('img'); image.src=url; image.alt=imageMatch[1]||''; image.loading='lazy'; image.decoding='async'; image.referrerPolicy='no-referrer';
                    if(imageMatch[3]) image.title=imageMatch[3]; parent.appendChild(image);
                }else appendText(parent,imageMatch[0]);
                i+=imageMatch[0].length; continue;
            }
            const linkMatch=text.slice(i).match(/^\[([^\]]+)\]\((\S+?)(?:\s+["']([^"']*)["'])?\)/);
            if(linkMatch){
                const url=safeUrl(linkMatch[2],false);
                if(url){
                    const link=document.createElement('a'); link.href=url;
                    if(!url.startsWith('#')){ link.target='_blank'; link.rel='noopener noreferrer'; }
                    if(linkMatch[3]) link.title=linkMatch[3]; appendInline(link,linkMatch[1],options,depth+1); parent.appendChild(link);
                }else appendText(parent,linkMatch[0]);
                i+=linkMatch[0].length; continue;
            }
            const citationMatch=text.slice(i).match(/^\[(\d+)\]/);
            if(citationMatch){
                const number=Number(citationMatch[1]);
                const citation=Array.isArray(options.citations) ? options.citations[number-1] : null;
                if(citation && typeof options.onCitation==='function'){
                    const button=document.createElement('button'); button.type='button'; button.className='knowledge-citation-link'; button.textContent=`[${number}]`;
                    button.title=`查看引用 ${number}：${citation.title||citation.section||'知识库片段'}`;
                    button.addEventListener('click',event=>{ event.stopPropagation(); options.onCitation(citation,number); });
                    parent.appendChild(button);
                }else appendText(parent,citationMatch[0]);
                i+=citationMatch[0].length; continue;
            }
            const markers=[['**','strong'],['__','strong'],['~~','del'],['*','em'],['_','em']];
            let marked=false;
            for(const [marker,tag] of markers){
                if(!text.startsWith(marker,i)) continue;
                const end=findClosing(text,marker,i+marker.length);
                if(end<=i+marker.length) continue;
                const node=document.createElement(tag); appendInline(node,text.slice(i+marker.length,end),options,depth+1); parent.appendChild(node);
                i=end+marker.length; marked=true; break;
            }
            if(marked) continue;
            const autoLink=text.slice(i).match(/^<(https?:\/\/[^ >]+|mailto:[^ >]+)>/i);
            const bareLink=text.slice(i).match(/^https?:\/\/[^\s<]+[^\s<.,;:!?)]/i);
            const urlToken=autoLink?.[1] || bareLink?.[0];
            if(urlToken){
                const link=document.createElement('a'); link.href=urlToken; link.target='_blank'; link.rel='noopener noreferrer'; link.textContent=urlToken; parent.appendChild(link);
                i+=(autoLink ? autoLink[0].length : bareLink[0].length); continue;
            }
            let next=i+1;
            while(next<text.length && !/[\\`*!_~\[<\n]/.test(text[next]) && !text.startsWith('http://',next) && !text.startsWith('https://',next)) next++;
            appendText(parent,text.slice(i,next)); i=next;
        }
    }

    function splitTableRow(line){
        let value=String(line||'').trim();
        if(value.startsWith('|')) value=value.slice(1);
        if(value.endsWith('|')) value=value.slice(0,-1);
        const cells=[]; let current=''; let escaped=false; let code=false;
        for(const char of value){
            if(escaped){ current+=char; escaped=false; continue; }
            if(char==='\\'){ escaped=true; current+=char; continue; }
            if(char==='`'){ code=!code; current+=char; continue; }
            if(char==='|'&&!code){ cells.push(current.trim()); current=''; } else current+=char;
        }
        cells.push(current.trim()); return cells;
    }

    function isTableSeparator(line){
        const cells=splitTableRow(line);
        return cells.length>0 && cells.every(cell=>/^:?-{3,}:?$/.test(cell));
    }

    function isBlockStart(lines,index){
        const line=lines[index]||'';
        return /^\s*(```|~~~)/.test(line) || /^\s{0,3}#{1,6}\s+/.test(line) || /^\s{0,3}>\s?/.test(line) || /^\s{0,3}([-+*])\s+/.test(line) || /^\s{0,3}\d+[.)]\s+/.test(line) || /^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$/.test(line) || (index+1<lines.length && line.includes('|') && isTableSeparator(lines[index+1]));
    }

    function createCodeBlock(codeText,language){
        const wrap=document.createElement('div'); wrap.className='chat-code-block';
        const head=document.createElement('div'); head.className='chat-code-head';
        const label=document.createElement('span'); label.textContent=language||'代码';
        const copy=document.createElement('button'); copy.type='button'; copy.className='chat-code-copy'; copy.textContent='复制';
        copy.addEventListener('click',async()=>{
            try{ await navigator.clipboard.writeText(codeText); copy.textContent='已复制'; setTimeout(()=>{ if(copy.isConnected) copy.textContent='复制'; },1200); }
            catch(e){ copy.textContent='复制失败'; }
        });
        head.append(label,copy);
        const pre=document.createElement('pre'); const code=document.createElement('code');
        if(language) code.className=`language-${String(language).replace(/[^a-z0-9_+-]/gi,'')}`;
        code.textContent=codeText; pre.appendChild(code); wrap.append(head,pre); return wrap;
    }

    function renderBlocks(target,markdown,options={}){
        const lines=String(markdown||'').replace(/\r\n?/g,'\n').split('\n');
        let i=0;
        while(i<lines.length){
            const line=lines[i];
            if(!line.trim()){ i++; continue; }
            const fence=line.match(/^\s*(```|~~~)\s*([^\s]*)\s*$/);
            if(fence){
                const marker=fence[1]; const code=[]; i++;
                while(i<lines.length&&!new RegExp(`^\\s*${marker}`).test(lines[i])) code.push(lines[i++]);
                if(i<lines.length) i++;
                target.appendChild(createCodeBlock(code.join('\n'),fence[2])); continue;
            }
            const heading=line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
            if(heading){ const node=document.createElement(`h${heading[1].length}`); appendInline(node,heading[2],options); target.appendChild(node); i++; continue; }
            if(/^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$/.test(line)){ target.appendChild(document.createElement('hr')); i++; continue; }
            if(/^\s{0,3}>\s?/.test(line)){
                const quoted=[]; while(i<lines.length&&/^\s{0,3}>\s?/.test(lines[i])) quoted.push(lines[i++].replace(/^\s{0,3}>\s?/,''));
                const quote=document.createElement('blockquote'); renderBlocks(quote,quoted.join('\n'),options); target.appendChild(quote); continue;
            }
            const listMatch=line.match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
            if(listMatch){
                const ordered=/\d/.test(listMatch[1][0]); const list=document.createElement(ordered?'ol':'ul');
                if(ordered) list.start=Number.parseInt(listMatch[1],10)||1;
                while(i<lines.length){
                    const match=lines[i].match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
                    if(!match || /\d/.test(match[1][0])!==ordered) break;
                    const item=document.createElement('li'); let content=match[2];
                    const task=content.match(/^\[([ xX])\]\s+(.*)$/);
                    if(task){ const checkbox=document.createElement('input'); checkbox.type='checkbox'; checkbox.checked=task[1].toLowerCase()==='x'; checkbox.disabled=true; item.className='task-list-item'; item.appendChild(checkbox); content=task[2]; }
                    appendInline(item,content,options); list.appendChild(item); i++;
                }
                target.appendChild(list); continue;
            }
            if(i+1<lines.length&&line.includes('|')&&isTableSeparator(lines[i+1])){
                const headers=splitTableRow(line); const alignments=splitTableRow(lines[i+1]).map(cell=>cell.startsWith(':')&&cell.endsWith(':')?'center':cell.endsWith(':')?'right':'left');
                const scroll=document.createElement('div'); scroll.className='chat-table-scroll'; const table=document.createElement('table'); const thead=document.createElement('thead'); const headerRow=document.createElement('tr');
                headers.forEach((cell,index)=>{ const th=document.createElement('th'); th.style.textAlign=alignments[index]||'left'; appendInline(th,cell,options); headerRow.appendChild(th); }); thead.appendChild(headerRow); table.appendChild(thead); i+=2;
                const tbody=document.createElement('tbody');
                while(i<lines.length&&lines[i].includes('|')&&lines[i].trim()){
                    const row=document.createElement('tr'); splitTableRow(lines[i]).forEach((cell,index)=>{ const td=document.createElement('td'); td.style.textAlign=alignments[index]||'left'; appendInline(td,cell,options); row.appendChild(td); }); tbody.appendChild(row); i++;
                }
                table.appendChild(tbody); scroll.appendChild(table); target.appendChild(scroll); continue;
            }
            const paragraph=[];
            while(i<lines.length&&lines[i].trim()&&!isBlockStart(lines,i)) paragraph.push(lines[i++]);
            if(!paragraph.length){ paragraph.push(lines[i++]); }
            const p=document.createElement('p'); appendInline(p,paragraph.join('\n'),options); target.appendChild(p);
        }
    }

    function render(target,markdown,options={}){
        if(!target) return;
        target.replaceChildren(); target.classList.add('chat-markdown');
        renderBlocks(target,markdown,options);
    }

    global.ChatMarkdown={render,safeUrl};
})(window);
