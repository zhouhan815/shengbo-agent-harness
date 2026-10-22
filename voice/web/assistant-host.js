(function(){
  'use strict';
  const panel=document.getElementById('assistant-panel');
  const toggle=document.getElementById('toggle-assistant');
  const currentView=()=>Array.from(document.querySelectorAll('main>.view')).find(n=>!n.hidden)?.id;
  let workspace;
  const assistant=ShengboAssistant.mount({container:panel,getViewContext(){
    const active=['view-room','view-monitor'].includes(currentView());
    return {room_id:active?roomId:null,selected_product_ids:active?[...chosenProducts]:[],view:currentView()};
  },onNavigate(destination){if(destination.view==='preparation')workspace.open(destination);else if(destination.view==='products')view('products');else if(destination.room_id)enterRoom(destination.room_id)}});
  workspace=ShengboPreliveWorkspace.mount({getConversationId:()=>assistant._inspect().conversation_id,onChange:()=>{refresh().catch(()=>{});assistant.refresh()},async onStarted(id){await refresh();await enterRoom(id);view('monitor');connectMonitor(id)}});
  document.getElementById('open-prelive-workspace').addEventListener('click',()=>workspace.open());
  document.getElementById('close-prelive-workspace').addEventListener('click',()=>document.getElementById('prelive-workspace').close());
  toggle.addEventListener('click',()=>{const open=!document.body.classList.contains('assistant-expanded');document.body.classList.toggle('assistant-expanded',open);toggle.setAttribute('aria-expanded',String(open))});
  window.shengboAssistant=assistant;
  window.addEventListener('assistant:select',e=>assistant.setSelection(e.detail));
})();
