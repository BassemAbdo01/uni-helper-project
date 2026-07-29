const $=id=>document.getElementById(id);
async function refresh(){
  const s=await chrome.storage.sync.get(['apiBase','token']);
  $('apiBase').value=s.apiBase||'';$('token').value=s.token||'';
  const result=await chrome.runtime.sendMessage({type:'NEXORA_STATUS'}).catch(()=>null);
  if(!result)return;
  const lr=result.lastResult||{};
  const courses=lr.courses||{}; const content=lr.content||{};
  $('status').textContent=[
    result.configured?'✓ Connected':'Not configured',
    result.lastSync?`Last activity: ${new Date(result.lastSync).toLocaleString()}`:'No import yet',
    courses.ok?`Courses: +${courses.added||0} / ${courses.updated||0} updated / ${courses.unchanged||0} unchanged`:'',
    content.ok?`Content: +${content.added||0} / ${content.updated||0} updated`:'',
    result.busy||result.queued?`Importing… ${result.queued||0} pages queued`:''
  ].filter(Boolean).join('\n');
}
refresh();
$('save').onclick=async()=>{const apiBase=$('apiBase').value.trim(),token=$('token').value.trim();await chrome.storage.sync.set({apiBase,token});$('status').textContent='Connection saved.';refresh();};
$('import').onclick=async()=>{await $('save').onclick();$('status').textContent='Opening Brightspace. Log in if asked; course discovery starts automatically.';await chrome.runtime.sendMessage({type:'NEXORA_START_IMPORT',url:'https://brightspace.carleton.ca/d2l/home'});window.close();};
$('scan').onclick=async()=>{const tabs=await chrome.tabs.query({url:['https://brightspace.carleton.ca/*','https://*.brightspace.com/*']});for(const tab of tabs){try{await chrome.tabs.sendMessage(tab.id,{type:'NEXORA_SCAN_FULL'})}catch(_){}}$('status').textContent=`Scanning ${tabs.length} open Brightspace page(s)…`;setTimeout(refresh,2500);};
