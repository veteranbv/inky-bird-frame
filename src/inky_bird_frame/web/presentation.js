// Owner-only controls. Screen cookies and status credentials are not owner logins.
export async function renderPresentation(ui, {base, link, note, data, area}) {
  const {api,element:el,button} = ui, main=document.querySelector('#main');
  base(area==='integrations' ? 'Integrations' : 'Displays', 'Choose what each destination may see. A browser never reports a physical panel update.');
  const state=await api('/v1/owner/presentation');
  const label = key => key==='current' ? 'Current and saved' : key==='saved' ? 'Saved birds' : key==='all' ? 'All available artwork' : key==='favorites' ? 'Favorites' : data.collections.find(group=>group.collection_id===key)?.label || 'Removed collection';
  const views=['current','saved','favorites','all',...data.collections.filter(group=>group.kind!=='favorites').map(group=>group.collection_id)];
  let serial=0, busy=false;
  function field(section, title, type='text', value='') {
    const label=el('label',title), input=el('input');input.type=type;input.id='presentation-'+ ++serial;label.htmlFor=input.id;input.value=value;
    if(type==='checkbox'){label.textContent='';label.className='check-label';label.append(input,document.createTextNode(title));section.append(label);}else section.append(label,input);return input;
  }
  function choice(section, selected='current') {
    const name=el('label','Permitted view'), input=el('select');input.id='view-'+ ++serial;name.htmlFor=input.id;
    for(const key of views){const option=el('option',label(key));option.value=key;option.selected=key===selected;input.append(option);}section.append(name,input);return input;
  }
  async function update(body, section, done=()=>location.reload()) {
    if(busy)return;busy=true;
    try { done(await api('/v1/owner/presentation',{revision:state.revision,confirm:true,...body})); }
    catch(error){if(error.code==='confirmation_required'){
      const notice=el('section','', 'notice');notice.append(note('Confirm it is you before granting or changing access. Your choices stay on this page.'));
      if(ui.localPasswordAllowed){const password=field(notice,'Your Inky password','password');password.autocomplete='current-password';
        notice.append(button('Confirm and retry',async()=>{try{await api('/v1/owner/confirm-password',{passphrase:password.value});password.value='';notice.remove();await update(body,section,done);}catch(failure){password.value='';notice.append(el('p',failure.message,'error'));}}));password.focus();}
      if(ui.singleSignOnReady)notice.append(button('Confirm with single sign-on',()=>ui.confirmWithSSO()),note('Sign-in returns to Account and access. Return here and repeat this change; unsaved entries will clear.'));
      notice.append(button('Cancel confirmation',()=>notice.remove()));section.append(notice);
    }else section.append(el('p',error.message,'error'));}finally{busy=false;}
  }
  main.append(link('Back to Manage','manage'));
  if(area==='displays') {
    main.append(el('h2','Browser screens'),note('Open /screen on the tablet or computer you want to pair. A pairing code connects a read-only screen; it is not part of normal owner sign-in.'),el('a','Open screen pairing'));
    main.lastChild.href='/screen';main.lastChild.className='text-link';
    for(const grant of state.grants.filter(grant=>grant.kind!=='status')) {
      const section=el('section','','journal-card');section.append(el('h2',grant.kind==='pending' ? 'Pairing request · '+grant.pairing_code : grant.name));
      if(grant.kind==='pending'){
        section.append(note('Match this code on the actual screen before granting access.'));
        const name=field(section,'Screen name','text',''), view=choice(section), days=field(section,'Access expires after days','number','90');name.maxLength=128;days.min='1';days.max='365';
        const consent=field(section,'I matched this code on the screen I want to pair','checkbox');
        section.append(button('Pair this screen',()=>{if(!consent.checked){section.append(el('p','Match the code and confirm first.','error'));return;}update({action:'pair',id:grant.id,name:name.value,view:view.value,days:Number(days.value)},section);}));
      }else section.append(note(label(grant.view)),note('Expires '+new Date(grant.expires_at*1000).toLocaleString()),note(grant.last_seen_at ? 'Last connection, hour precision: '+new Date(grant.last_seen_at*1000).toLocaleString() : 'Not connected yet'));
      section.append(button(grant.kind==='pending' ? 'Remove request' : 'Revoke screen',()=>update({action:'revoke',id:grant.id},section)),note('Revocation clears an online screen on its next check. An offline screen cannot receive it until it reconnects.'));main.append(section);
    }
    const wall=el('section','','journal-card');wall.append(el('h2','Physical wall panel'),note('Selected route: '+label(state.wall_view)),note('This applies on its next scheduled refresh only if the display uses the selected-wall address. Existing /v1/catalog clients remain unchanged.'));
    const view=choice(wall,state.wall_view), consent=field(wall,'I want this view on the selected-wall route','checkbox');
    wall.append(button('Confirm wall selection',()=>{if(consent.checked)update({action:'wall',view:view.value},wall);else wall.append(el('p','Confirm the wall change first.','error'));}),note('One-time operator setup: set display_node.controller_url to '+location.origin+'/wall, then restart the display service. This page does not change that device configuration.'),note('Last confirmed panel update: '+(data.management.last_confirmed_panel_update ? new Date(data.management.last_confirmed_panel_update).toLocaleString() : 'Not received')));main.append(wall);
    return;
  }
  const art=el('section','','journal-card');art.append(el('h2','Dashboard artwork'),note(state.dashboard_view ? 'On · '+label(state.dashboard_view) : 'Off'),note('This is a minimal art page, without owner cookies, private labels, dates, source names or detection details. Anyone who can reach its permitted network address can learn the selected species. Framing restrictions do not authenticate a visitor.'));
  const view=choice(art,state.dashboard_view||'current'), consent=field(art,'I approve exposing this selected species membership on my trusted network or VPN','checkbox');
  art.append(button('Enable this art view',()=>{if(consent.checked)update({action:'dashboard',view:view.value},art);else art.append(el('p','Read and confirm the exposure first.','error'));}));
  if(state.dashboard_view)art.append(button('Turn dashboard art off',()=>update({action:'dashboard-off'},art)));
  art.append(note('Permitted framing origins: '+(state.embed_allowed_origins.join(', ')||'This Inky origin only. Configure controller.embed_allowed_origins and restart for a separate dashboard.')),note('An HTTPS dashboard needs an HTTPS Inky address. A remote phone still needs a private route or VPN. Never disable certificate checks or expose owner paths just to make an embed work.'));
  const mixedContent=location.protocol==='http:' && state.embed_allowed_origins.some(origin=>origin.startsWith('https:'));
  if(mixedContent)art.append(el('p','Your configured dashboard uses HTTPS, but this Inky address uses HTTP. The browser will block this art recipe. Open Inky through its private HTTPS address before copying it. Status sensors still work server-side.','notice'));
  const recipe=el('pre');recipe.textContent='Home Assistant webpage card:\ntype: iframe\nurl: '+location.origin+'/v1/dashboard/art\naspect_ratio: 133%\n\nHomepage iframe widget:\nwidget:\n  type: iframe\n  src: '+location.origin+'/v1/dashboard/art';if(!mixedContent)art.append(recipe);main.append(art);
  const status=el('section','','journal-card');status.append(el('h2','Status sensors'),note('A separate read-only credential exposes only version, ready counts, coarse discovery state and panel health. It cannot read collections, histories, source credentials or images. It expires and can be revoked here.'));
  const name=field(status,'Credential name','text','Dashboard status'), permitted=choice(status), days=field(status,'Credential expires after days','number','90');days.min='1';days.max='365';name.maxLength=128;
  status.append(button('Create status credential',()=>update({action:'status',name:name.value,view:permitted.value,days:Number(days.value)},status,result=>{
    state.revision+=1;const once=el('section','','notice'), secret=el('pre');secret.textContent=result.secret;
    once.append(el('h2','Copy this credential now'),note('It is shown once. Store it in your dashboard secret store. Reloading loses this copy; revoke and create another if needed.'),secret);
    const config=el('pre');config.textContent='GET '+location.origin+'/v1/integrations/status\nAuthorization: Bearer <saved status credential>\n\nHome Assistant:\nrest:\n  - resource: '+location.origin+'/v1/integrations/status\n    headers:\n      Authorization: !secret inky_status_authorization\n    scan_interval: 60\n    sensor:\n      - name: Inky ready plates\n        value_template: "{{ value_json.data.view_ready_count }}"\n\nHomepage customapi:\nwidget:\n  type: customapi\n  url: '+location.origin+'/v1/integrations/status\n  headers:\n    Authorization: "Bearer {{HOMEPAGE_VAR_INKY_STATUS}}"\n  mappings:\n    - field: data.view_ready_count\n      label: Plates\n      format: number';once.append(config,button('I saved it · clear this copy',()=>location.reload()));status.append(once);
  })));
  for(const grant of state.grants.filter(grant=>grant.kind==='status'))status.append(el('h3',grant.name),note(label(grant.view)+' · expires '+new Date(grant.expires_at*1000).toLocaleString()),button('Revoke '+grant.name,()=>update({action:'revoke',id:grant.id},status)));
  main.append(status,link('Notification delivery settings','notifications'));
}
