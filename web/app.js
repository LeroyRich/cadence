const fields = ["title", "artist", "album", "album_artist", "track", "disc", "date", "genre", "comment"];
const labels = {title:"Title",artist:"Artist",album:"Album",album_artist:"Album artist",track:"Track",disc:"Disc",date:"Date",genre:"Genre",comment:"Comment"};
const state = {csrf:"",items:[],knownTracks:new Map(),selected:new Set(),queue:new Map(),offset:0,pageSize:100,trackRequest:0,total:0,root:"",query:"",sort:"",direction:"asc",editing:[],draft:{},touchedFields:new Set(),artwork:null,artworkRequest:0,plan:null,view:"library",poll:null,jobDetail:null};
Object.assign(state,{trackFilter:"all",genreKey:"",genreChoicesRequest:0,listRevision:0,autoId3Upgrade:true,editorFollowsSelection:false,selectionAnchor:null});
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
const fmtDuration = value => `${Math.floor(value/60)}:${String(value%60).padStart(2,"0")}`;
const formatTimestamp = value => value == null ? "Unavailable" : new Date(value*1000).toLocaleString();
const maxSelection = 5000;
const columns = [
  {id:"title",label:"Title / File",sort:"title",width:220,min:140},
  {id:"path",label:"Path",sort:"path",width:190,min:120},
  {id:"artist",label:"Artist",sort:"artist",width:130,min:100},
  {id:"album",label:"Album",sort:"album",width:130,min:100},
  {id:"track",label:"Track",sort:"track",width:86,min:86,fixed:true},
  {id:"disc",label:"Disc",sort:"disc",width:78,min:78,fixed:true},
  {id:"format",label:"Format",sort:"format",width:105,min:105,fixed:true},
  {id:"duration",label:"Length",sort:"duration",width:88,min:88,fixed:true}
];
let columnOrder=columns.map(column=>column.id);
let hiddenColumns=new Set();
let columnWidths=Object.fromEntries(columns.map(column=>[column.id,column.width]));
let floatingHeader;
function folderView(){return state.trackFilter==="empty_folders"}
function visibleColumns(){return columnOrder.filter(id=>!hiddenColumns.has(id)||(folderView()&&["title","path"].includes(id))).map(id=>columns.find(column=>column.id===id))}
function saveColumnSettings(){try{localStorage.setItem("cadence-columns-v2",JSON.stringify({order:columnOrder,hidden:[...hiddenColumns],widths:columnWidths}))}catch{}}
function loadColumnSettings(){
  try{
    const saved=JSON.parse(localStorage.getItem("cadence-columns-v2"));
    const ids=columns.map(column=>column.id);
    if(saved&&Array.isArray(saved.order)&&saved.order.length&&new Set(saved.order).size===saved.order.length&&saved.order.every(id=>ids.includes(id))){
      columnOrder=[...saved.order,...ids.filter(id=>!saved.order.includes(id))];
    }
    if(saved&&Array.isArray(saved.hidden)&&saved.hidden.every(id=>ids.includes(id))&&saved.hidden.length<ids.length)hiddenColumns=new Set(saved.hidden);
    if(saved&&saved.widths)for(const column of columns)if(!column.fixed&&Number.isFinite(saved.widths[column.id])&&saved.widths[column.id]>=column.min&&saved.widths[column.id]<=2000)columnWidths[column.id]=saved.widths[column.id];
    if(!saved){
      const previous=JSON.parse(localStorage.getItem("cadence-column-widths-v1"));
      if(Array.isArray(previous)&&previous.length===7)["title","path","artist","album"].forEach((id,index)=>{const column=columns.find(item=>item.id===id);if(Number.isFinite(previous[index+1])&&previous[index+1]>=column.min&&previous[index+1]<=2000)columnWidths[id]=previous[index+1]});
    }
  }catch{}
}
function headerMarkup(){return `<tr><th class="check-col"><input id="select-page" type="checkbox" aria-label="Select visible ${folderView()?"folders":"tracks"}"></th>${visibleColumns().map(column=>`<th data-column="${column.id}" class="${column.id==="path"?"path-col":column.id==="duration"?"duration-col":""}" aria-sort="none"><button class="sort-button" data-sort="${column.sort}" ${folderView()&&!["title","path"].includes(column.id)?"disabled":""}>${column.label.toUpperCase()} <span class="sort-indicator" aria-hidden="true">↕</span></button>${column.fixed?"":`<button type="button" class="column-resize" data-column="${column.id}" aria-label="Resize ${column.label} column" title="Drag to resize column · Arrow keys for fine adjustment"></button>`}</th>`).join("")}<th class="row-fill" aria-hidden="true"></th></tr>`}
function renderColumnHeaders(){
  const table=$("#library-table");
  table.tHead.innerHTML=headerMarkup();
  const floatingTable=floatingHeader.querySelector("table");
  floatingTable.tHead.innerHTML=headerMarkup();
  floatingTable.querySelector("#select-page").id="select-page-sticky";
  applyColumnWidths();
  updateSortHeaders();
  syncSelection();
}
function applyColumnWidths(){
  for(const table of [$("#library-table"),floatingHeader?.querySelector("table")]){
    if(!table)continue;
    let group=table.querySelector("colgroup");
    if(!group){group=document.createElement("colgroup");table.prepend(group)}
    const widths=[48,...visibleColumns().map(column=>columnWidths[column.id])];
    group.replaceChildren(...widths.map(width=>{const element=document.createElement("col");element.style.width=`${width}px`;return element}));
    // Let only the trailing cell absorb spare space, preserving user column widths.
    group.append(document.createElement("col"));
    table.style.width=`max(100%, ${widths.reduce((sum,width)=>sum+width,0)}px)`;
  }
  syncFloatingHeader();
}
function syncFloatingHeader(){
  if(!floatingHeader)return;
  const wrap=$(".table-wrap"),header=$("#library-table thead"),bar=$(".library-controls");
  const wrapRect=wrap.getBoundingClientRect(),headerRect=header.getBoundingClientRect();
  if(headerRect.height)wrap.style.setProperty("--header-fill-height",`${headerRect.height}px`);
  const top=Math.max(0,Math.floor(bar.getBoundingClientRect().bottom));
  const visible=state.view==="library"&&!$("#app").classList.contains("hidden")&&
    headerRect.top<=top&&wrapRect.top<=top&&wrapRect.bottom>top+headerRect.height;
  floatingHeader.classList.toggle("hidden",!visible);
  if(!visible)return;
  floatingHeader.style.top=`${top}px`;
  floatingHeader.style.left=`${wrapRect.left}px`;
  floatingHeader.style.width=`${wrapRect.width}px`;
  floatingHeader.querySelector("table").style.transform=`translateX(${-wrap.scrollLeft}px)`;
}
function setupLibraryTable(){
  if(floatingHeader)return;
  const table=$("#library-table"),wrap=$(".table-wrap");
  floatingHeader=document.createElement("div");
  floatingHeader.className="floating-table-header hidden";
  const floatingTable=document.createElement("table");
  floatingTable.append(table.tHead.cloneNode(true));
  floatingTable.querySelector("#select-page").id="select-page-sticky";
  const floatingSurface=document.createElement("div");
  floatingSurface.className="floating-header-surface";
  floatingSurface.append(floatingTable);
  floatingHeader.append(floatingSurface);
  document.body.append(floatingHeader);
  loadColumnSettings();
  renderColumnHeaders();
  wrap.addEventListener("scroll",syncFloatingHeader,{passive:true});
  window.addEventListener("scroll",syncFloatingHeader,{passive:true});
  window.addEventListener("resize",syncFloatingHeader);
}
function toast(message){const node=$("#toast");node.textContent=message;node.classList.remove("hidden");clearTimeout(toast.timer);toast.timer=setTimeout(()=>node.classList.add("hidden"),4500)}
async function api(path, body){const options={headers:{}};if(body!==undefined){options.method="POST";options.headers["Content-Type"]="application/json";options.headers["X-CSRF-Token"]=state.csrf;options.body=JSON.stringify(body)}const response=await fetch(path,options);const data=await response.json();if(!response.ok)throw new Error(data.error||"Request failed");return data}
function setQueue(items){state.queue=new Map(items.map(item=>[item.path,item]));$("#process-queue").disabled=!items.length;$("#process-queue").innerHTML=`Process queue (${items.length.toLocaleString()}) <span>→</span>`}
async function loadQueue(){const {items}=await api("/api/queue");setQueue(items)}
function showView(view){if(view==="library"&&folderView())state.folderScope=null;state.view=view;document.querySelectorAll(".nav-item").forEach(node=>node.classList.toggle("active",node.dataset.view===view));document.querySelectorAll(".view").forEach(node=>node.classList.toggle("hidden",node.id!==`${view}-view`));$("#page-title").textContent={library:"Library",activity:"Activity",settings:"Settings"}[view];if(view==="library"){loadQueue().then(refreshFilteredTracks).catch(error=>toast(error.message))}if(view==="activity")loadActivity();if(view==="settings")loadSettings();requestAnimationFrame(syncFloatingHeader)}
async function refreshRoots(){const {roots}=await api("/api/roots");$("#folder-list").innerHTML=roots.length?roots.map(root=>`<button class="folder-item ${state.root===root?"active":""}" data-root="${esc(root)}" title="${esc(root)}"><span>▣</span><span class="name">${esc(root.split("/").pop())}</span><span class="remove" data-remove="${esc(root)}" title="Remove folder">×</span></button>`).join(""):`<span class="folder-item">No folders yet</span>`;$("#root-filter").innerHTML=`<option value="">All libraries</option>`+roots.map(root=>`<option value="${esc(root)}">${esc(root)}</option>`).join("");$("#root-filter").value=state.root}
function updateSortHeaders(){document.querySelectorAll(".sort-button").forEach(button=>{const active=state.sort===button.dataset.sort;button.closest("th").setAttribute("aria-sort",active?(state.direction==="asc"?"ascending":"descending"):"none");button.querySelector(".sort-indicator").textContent=active?(state.direction==="asc"?"↑":"↓"):"↕"})}
function libraryFilters(){return {q:state.query,root:state.root,filter:state.trackFilter,genre:state.genreKey}}
function libraryFilterKey(){return JSON.stringify(libraryFilters())}
function syncPagination(range){
  const pageCount=Math.ceil(state.total/state.pageSize);
  const currentPage=Math.floor(state.offset/state.pageSize)+1;
  const pageOptions=pageCount?Array.from({length:pageCount},(_,index)=>{
    const page=index+1;
    return `<option value="${page}">${page===currentPage?`Page ${page}`:page}</option>`;
  }).join(""):'<option value="">No pages</option>';
  for(const id of ["page-size","page-size-top"])$("#"+id).value=String(state.pageSize);
  for(const id of ["page-jump","page-jump-top"]){
    const control=$("#"+id);
    control.innerHTML=pageOptions;
    control.disabled=!pageCount;
    if(pageCount)control.value=String(currentPage);
  }
  for(const id of ["range-label","range-label-top"])$("#"+id).textContent=range;
  for(const id of ["previous-page","previous-page-top"])$("#"+id).disabled=state.offset===0;
  for(const id of ["next-page","next-page-top"])$("#"+id).disabled=state.offset+state.pageSize>=state.total;
}
function resetFilteredSelection(){
  closeEditor();
  state.selected.clear();state.selectionAnchor=null;state.listSelection=null;state.listRevision++;state.trackRequest++;state.offset=0;
  state.items=[];state.total=0;$("#track-rows").innerHTML="";syncPagination("Loading…");syncSelection();
}
async function applyLibraryFilter(){
  const wasFolders=folderView();
  state.trackFilter=$("#track-filter").value;
  if(folderView()&&!wasFolders){state.trackSort={sort:state.sort,direction:state.direction};state.sort="path";state.direction="asc";state.folderScope=null}
  if(!folderView()&&wasFolders){Object.assign(state,state.trackSort||{sort:"",direction:"asc"});state.folderScanToken=(state.folderScanToken||0)+1;state.folderLoading=false;$("#folder-scan-status").classList.add("hidden")}
  renderColumnHeaders();
  $("#search").placeholder=folderView()?"Search folder names or paths":"Search titles, artists, albums or paths";
  $("#genre-pattern").classList.toggle("hidden",state.trackFilter!=="genre_search");
  state.genreKey="";
  if(state.trackFilter!=="genre_search")state.genreChoicesRequest++;
  $("#filter-hint").textContent=({no_album_folder:"Files directly inside an artist folder beneath the library root.",missing_year:"No recognizable year in the Date tag.",invalid_characters:"Hidden control or replacement characters in saved tags.",genre_search:"Choose a genre to find tracks with that saved tag."})[state.trackFilter]||"";
  resetFilteredSelection();
  try{await refreshFilteredTracks()}catch(error){toast(error.message)}
}
async function refreshGenreChoices(){
  const request=++state.genreChoicesRequest,root=state.root,selected=state.genreKey,control=$("#genre-filter");
  control.disabled=true;
  control.innerHTML='<option value="">Loading genres…</option>';
  try{
    const {genres}=await api(`/api/genres/list?${new URLSearchParams({root})}`);
    if(request!==state.genreChoicesRequest||root!==state.root||state.trackFilter!=="genre_search")return;
    control.innerHTML='<option value="">Choose a genre</option>'+genres.map(item=>`<option value="${esc(item.genre_key)}">${esc(item.genre)} (${item.count.toLocaleString()})</option>`).join("");
    control.value=selected;
    if(control.value!==selected){state.genreKey="";resetFilteredSelection()}
    control.disabled=!genres.length;
    if(!genres.length)control.innerHTML='<option value="">No indexed genres</option>';
  }catch(error){
    if(request===state.genreChoicesRequest){control.innerHTML='<option value="">Could not load genres</option>';state.genreKey="";toast(error.message)}
  }
}
async function refreshFilteredTracks(){
  if(folderView()&&state.folderScope!==state.root){await refreshEmptyFolders();return}
  if(state.trackFilter==="genre_search")await refreshGenreChoices();
  await refreshTracks();
}
async function refreshEmptyFolders(){
  const token=state.folderScanToken=(state.folderScanToken||0)+1,root=state.root;
  state.folderScope=root;state.folderLoading=true;resetFilteredSelection();
  $("#indexed-label").textContent="EMPTY FOLDERS";$("#track-count").textContent="—";
  const status=$("#folder-scan-status");status.classList.remove("hidden");status.textContent="Finding empty folders…";
  $("#empty-state").classList.add("hidden");
  try{
    const {job}=await api("/api/empty-folders/refresh",{root});
    while(token===state.folderScanToken&&folderView()&&root===state.root){
      const data=await api(`/api/jobs/detail?${new URLSearchParams({id:job,limit:1})}`);
      if(token!==state.folderScanToken||!folderView()||root!==state.root)return;
      if(!["queued","running","cancelling"].includes(data.job.state)){
        state.folderLoading=false;
        const complete=data.job.state==="complete";
        status.classList.toggle("hidden",complete);
        status.textContent=complete?"":`Folder refresh ${data.job.state}. ${data.job.errors.map(error=>typeof error==="string"?error:error.error).join("; ")||"Run Scan folders to retry."}`;
        await refreshTracks();return;
      }
      status.textContent=data.job.state==="queued"?"Folder refresh is waiting for the current job…":"Finding empty folders…";
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
  }catch(error){if(token===state.folderScanToken&&folderView()){state.folderLoading=false;state.folderScope=null;status.textContent=`Could not refresh empty folders: ${error.message}`}}
}
async function refreshTracks(){
  if(folderView()&&state.folderLoading)return;
  const request=++state.trackRequest;
  const params=new URLSearchParams({...libraryFilters(),offset:state.offset,limit:state.pageSize,sort:state.sort||"path",direction:state.direction});
  const data=await api(`${folderView()?"/api/empty-folders":"/api/tracks"}?${params}`);
  if(request!==state.trackRequest)return;
  if(state.offset&&state.offset>=data.total){state.offset=data.total?Math.floor((data.total-1)/state.pageSize)*state.pageSize:0;return refreshTracks()}
  state.items=data.items;state.total=data.total;
  $("#genre-scan-hint").classList.toggle("hidden",!data.genres_need_scan);
  data.items.forEach(item=>state.knownTracks.set(item.path,item));
  $("#indexed-label").textContent=folderView()?"EMPTY FOLDERS":"TRACKS INDEXED";
  $("#track-count").textContent=data.total.toLocaleString();
  syncPagination(data.total?`${state.offset+1}–${Math.min(state.offset+data.items.length,data.total)} of ${data.total.toLocaleString()}`:folderView()?"0 folders":"0 tracks");
  $("#track-rows").innerHTML=data.items.map(item=>{
    const image=item.item_kind==="folder"?'<svg class="folder-icon" viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 6h7l2 2h9v12H3z"/><path d="M3 6V4h7l2 2h7v2"/></svg>':item.artwork?`<img src="/api/art/image?${esc(new URLSearchParams({path:item.path,version:item.mtime_ns}))}" alt="" loading="lazy" decoding="async">`:"♫";
    const cells={
      title:`<span class="title-cell"><span class="art-thumb" aria-hidden="true">${image}</span><span class="title-copy"><span class="track-title">${esc(item.name||item.tags.title||item.path.split("/").pop())}</span>${state.queue.has(item.path)?`<span class="queued-label">● ${state.queue.get(item.path).operation==="delete"?"Queued deletion":"Queued change"}</span>`:""}</span></span>`,
      path:`<span class="relative-path" title="${esc(item.relative_path)}">${esc(item.relative_path)}</span>`,
      artist:esc(item.tags.artist||"—"),album:esc(item.tags.album||"—"),
      track:esc(item.tags.track||"—"),disc:esc(item.tags.disc||"—"),
      format:`<span class="format-badge">${esc(item.format)}</span>`,duration:fmtDuration(item.duration)
    };
    if(item.item_kind==="folder")for(const key of ["artist","album","track","disc","format","duration"])cells[key]="";
    return `<tr data-path="${esc(item.path)}" class="${state.selected.has(item.path)?"selected":""} ${state.queue.has(item.path)?"queued":""}"><td class="check-col"><input type="checkbox" class="row-check" data-path="${esc(item.path)}" ${state.selected.has(item.path)?"checked":""} aria-label="Select ${esc(item.path)}"></td>${visibleColumns().map(column=>`<td class="${column.id==="path"?"path-col":column.id==="duration"?"duration-col":""}">${cells[column.id]}</td>`).join("")}<td class="row-fill" aria-hidden="true"></td></tr>`;
  }).join("");
  $("#empty-state").classList.toggle("hidden",data.items.length>0);
  $("#empty-state h3").textContent=folderView()?"No empty folders":state.trackFilter==="genre_search"?(state.genreKey?"No tracks use this genre":"Choose a genre"):"No tracks here yet";
  $("#empty-state p").textContent=folderView()?"Only folders with no entries appear here. Change your search or run Scan folders to refresh.":state.trackFilter==="genre_search"?(state.genreKey?"Try another genre or folder.":"Select a genre above to find its tracks."):"Add a folder and scan it, or change your search.";
  $("#empty-add").classList.toggle("hidden",folderView()||state.trackFilter==="genre_search");
  updateSortHeaders();syncSelection();requestAnimationFrame(syncFloatingHeader);
}
function syncSelection(){
  if(!state.selected.has(state.selectionAnchor)||!state.items.some(item=>item.path===state.selectionAnchor))state.selectionAnchor=null;
  $("#selected-count").textContent=`${state.selected.size.toLocaleString()} selected`;
  $("#edit-selected").disabled=!state.selected.size;
  const overListLimit=state.total>maxSelection;
  const selectList=$("#select-list"),tooltip=$("#select-list-tooltip");
  selectList.disabled=!state.total||overListLimit||Boolean(state.selectingList);
  tooltip.title=overListLimit?"This list exceeds the 5,000-item batch limit. Narrow the search or library filter to select the entire list.":"";
  tooltip.classList.toggle("over-limit",overListLimit);
  tooltip.tabIndex=overListLimit?0:-1;
  const cached=state.listSelection;
  const cachedPaths=cached?.key===libraryFilterKey()&&cached.paths.length===state.total&&
    state.items.every(item=>cached.pathSet.has(item.path))?cached.paths:null;
  const listPaths=cachedPaths||(state.items.length===state.total?state.items.map(item=>item.path):null);
  const allSelected=listPaths?.length>0&&listPaths.every(path=>state.selected.has(path));
  selectList.textContent=state.selectingList?"Updating selection…":
    `${allSelected?"Deselect":"Select"} entire list (${state.total.toLocaleString()})`;
  const visibleSelected=state.items.filter(item=>state.selected.has(item.path)).length;
  document.querySelectorAll("#select-page,#select-page-sticky").forEach(box=>{box.checked=state.items.length>0&&visibleSelected===state.items.length;box.indeterminate=visibleSelected>0&&visibleSelected<state.items.length});
  document.querySelectorAll("#track-rows tr").forEach(row=>{const selected=state.selected.has(row.dataset.path);row.classList.toggle("selected",selected);row.querySelector(".row-check").checked=selected});
  syncEditorSelection();
}
function syncEditorSelection(){
  if($("#inspector").classList.contains("hidden"))return;
  const paths=[...state.selected];
  if(state.editorFollowsSelection){
    if(!paths.length){closeEditor();return}
  }else if(paths.length<2)return;
  if(paths.length===state.editing.length&&paths.every(path=>state.editing.includes(path)))return;
  openEditor(paths,true);
}
function addSelection(paths){
  const selected=new Set(state.selected);
  paths.forEach(path=>selected.add(path));
  if(selected.size>maxSelection){toast("Select no more than 5,000 items at once. Clear the selection or narrow the list.");syncSelection();return false}
  state.selected=selected;
  syncSelection();
  return true;
}
function selectTrackRange(path){
  const start=state.items.findIndex(item=>item.path===state.selectionAnchor);
  const end=state.items.findIndex(item=>item.path===path);
  if(end<0)return;
  if(start<0){if(addSelection([path]))state.selectionAnchor=path;return}
  addSelection(state.items.slice(Math.min(start,end),Math.max(start,end)+1).map(item=>item.path));
}
async function selectEntireList(){
  if(!state.total||state.total>maxSelection||state.selectingList)return;
  const filters=libraryFilters(),key=libraryFilterKey(),revision=state.listRevision,sort=state.sort||"path",direction=state.direction;
  state.selectingList=true;
  syncSelection();
  try{
    const params=new URLSearchParams({...filters,sort,direction});
    const {paths}=await api(`${folderView()?"/api/empty-folders/paths":"/api/tracks/paths"}?${params}`);
    if(revision!==state.listRevision||key!==libraryFilterKey()){return}
    state.listSelection={key,paths,pathSet:new Set(paths)};
    if(paths.length&&paths.every(path=>state.selected.has(path))){
      paths.forEach(path=>state.selected.delete(path));
    }else{
      state.selected=new Set(paths);
    }
    syncSelection();
  }catch(error){toast(error.message)}
  finally{state.selectingList=false;syncSelection()}
}
function openEditor(paths,followSelection=false){
  stopTrackPreview();
  state.editing=paths;
  state.editorFollowsSelection=followSelection;
  state.draft={};
  state.touchedFields=new Set();
  state.artwork=null;
  state.artworkRequest++;
  state.plan=null;
  state.modeChanged=false;
  state.renameChanged=false;
  const queued=paths.map(path=>state.queue.get(path));
  state.editRevisions=Object.fromEntries(paths.map(path=>[path,state.queue.get(path)?.revision||null]));
  const track=state.knownTracks.get(paths[0]);
  state.editKind=track?.item_kind||"track";
  setupDeletionEditor(queued);
  if(state.editKind==="folder"){
    $("#inspector").classList.remove("hidden");
    $("#inspector-title").textContent=paths.length===1?"Edit folder":`Edit ${paths.length} folders`;
    $("#inspector-summary").classList.remove("hidden");$("#inspector-summary").textContent=`${paths.length} empty folder${paths.length===1?"":"s"} selected`;
    $("#inspector-path").classList.remove("hidden");$("#inspector-path strong").textContent="Folder paths";$("#inspector-path span").textContent=paths.join("\n");
    $(".inspector-scroll").scrollTop=0;return;
  }
  $("#inspector-path strong").textContent="File path";
  if(paths.length===1){state.draft={...(queued[0]?.edits||{})}}
  else for(const field of fields){const values=queued.map(item=>item?.edits?.[field]);if(values.length&&values[0]!==undefined&&values.every(value=>value!==undefined&&sameTag(field,value,values[0])))state.draft[field]=values[0]}
  $("#inspector").classList.remove("hidden");
  $("#inspector-title").textContent=paths.length===1?"Edit track":`Edit ${paths.length} tracks`;
  $("#inspector-summary").classList.toggle("hidden",paths.length===1);
  $("#inspector-summary").textContent=paths.length===1?"":`${paths.length} selected tracks · Shared values are shown; only changed fields are applied`;
  $("#inspector-path").classList.toggle("hidden",paths.length!==1);
  $("#inspector-path span").textContent=paths.length===1?paths[0]:"";
  function fieldMarkup(field){
    const pending=state.draft[field];
    const value=paths.length===1?(pending!==undefined?pending:track?.tags[field]||""):pending||"";
    const clear=paths.length>1&&pending==="";
    const queuedField=queued.some(item=>item?.edits&&Object.prototype.hasOwnProperty.call(item.edits,field));
    const placeholder=paths.length>1?"Checking shared values…":"";
    const markup=`<div class="field ${["title","artist","album","comment"].includes(field)?"full":""}"><div class="field-heading"><label for="field-${field}">${labels[field]} ${queuedField&&pending!==undefined?'<span class="pending-field">Queued</span>':""}</label>${paths.length>1?`<label class="clear-toggle"><input type="checkbox" data-clear="${field}" ${clear?"checked":""}> Clear</label>`:""}</div><input id="field-${field}" data-field="${field}" type="text" value="${esc(value)}" ${clear?"disabled":""} placeholder="${paths.length>1?placeholder:""}"></div>`;
    if(field!=="title")return markup;
    return `${markup}<details class="advanced-track-options"><summary>Advanced Track Options</summary><div class="advanced-track-body"><label class="normalize-control" for="normalize-title"><input id="normalize-title" type="checkbox"><span>Normalize titles <small>Capitalize each word and lowercase the rest.</small></span></label><label class="title-removal" for="remove-title-text">Remove exact text from titles<input id="remove-title-text" type="text" maxlength="1000" placeholder="e.g.  (Live)" autocomplete="off"></label><p>Case-sensitive; removes every occurrence from each selected title. Removal happens before normalization.</p></div></details>`;
  }
  $("#artist-fields").innerHTML=fieldMarkup("artist");
  $("#album-fields").innerHTML=["album","album_artist","track","disc","date"].map(fieldMarkup).join("");
  $("#title-fields").innerHTML=fieldMarkup("title");
  $("#genre-fields").innerHTML=genreEditorMarkup();
  $("#comment-fields").innerHTML=fieldMarkup("comment");
  setupGenreEditor(queued);
  const commonMode=queued.length&&queued.every(item=>item&&item.mode===queued[0].mode)?queued[0].mode:"none";
  const commonRename=queued.length&&queued.every(item=>item&&item.rename===queued[0].rename)?queued[0].rename:"none";
  $("#organize-mode").value=commonMode;
  $("#rename-mode").value=commonRename;
  $("#import-tags").value="none";
  $("#normalize-title").checked=false;
  $("#remove-title-text").value="";
  $("#candidate-list").innerHTML="";
  $("#cover-preview").textContent="♫";
  $("#artwork-status").textContent=paths.length===1?"Checking embedded artwork…":"Checking shared artwork…";
  $("#artwork-input").value="";
  $("#artwork-url").value="";
  $("#fetch-artwork").disabled=false;
  $("#fetch-artwork").textContent="Use URL";
  $("#fetch-artwork").dataset.request="";
  $("#match-text").disabled=paths.length!==1;
  $("#match-fingerprint").disabled=paths.length!==1;
  $("#track-preview").classList.toggle("hidden",paths.length!==1);
  if(paths.length===1)$("#preview-audio").src=`/api/audio?${new URLSearchParams({path:paths[0]})}`;
  if(paths.length===1){if(queued[0]?.artwork_id)loadQueuedArtwork(paths[0]);else loadArtwork(paths[0])}else loadCommonArtwork(paths)
  $(".inspector-scroll").scrollTop=0;
  loadBatchCommon(paths);
  syncDeletionControls();
}
async function loadBatchCommon(paths){
  try{
    const {common}=await api("/api/tracks/common",{paths});
    if(state.editing!==paths||$("#inspector").classList.contains("hidden"))return;
    for(const field of fields){
      if(field==="genre"){
        if(!state.touchedFields.has(field))setEditorGenres(common.genre,!Object.prototype.hasOwnProperty.call(common,"genre"));
        continue;
      }
      if(paths.length===1)continue;
      const input=$(`#field-${field}`);
      if(state.touchedFields.has(field)||input.disabled)continue;
      if(Object.prototype.hasOwnProperty.call(common,field)){
        input.value=common[field];
        input.placeholder=common[field]===""?"Empty in all tracks":"";
      }else{
        input.value="";
        input.placeholder="Mixed values · blank keeps each value";
      }
    }
  }catch(error){
    if(state.editing===paths&&!$("#inspector").classList.contains("hidden")){$("#genre-status").textContent="Could not load genres · reopen to retry";toast(error.message)}
  }
}
async function loadCommonArtwork(paths){
  const request=state.artworkRequest;
  const current=()=>request===state.artworkRequest&&state.editing===paths&&!$("#inspector").classList.contains("hidden");
  let expected=null,artwork=null,queued=false,indexedChanged=false;
  try{
    for(let offset=0;offset<paths.length;offset+=25){
      const result=await api("/api/art/common",{paths:paths.slice(offset,offset+25),expected});
      if(!current())return;
      indexedChanged||=result.indexed_changed;
      if(result.state==="mixed"){$("#artwork-status").textContent="Mixed artwork";return}
      if(result.state==="unavailable"){$("#artwork-status").textContent="Embedded artwork exists, but cannot be previewed for every selected track";return}
      expected=result.signature;artwork||=result.artwork;queued||=result.queued;
      if(offset+25<paths.length)$("#artwork-status").textContent=`Checking shared artwork… ${Math.min(offset+25,paths.length)} of ${paths.length}`;
    }
    if(artwork){
      $("#cover-preview").innerHTML=`<img alt="Shared album artwork" src="data:${artwork.mime};base64,${artwork.data}">`;
      $("#artwork-status").textContent=queued?"Shared artwork · Includes queued replacements":"Shared artwork across selected tracks";
    }else $("#artwork-status").textContent="No embedded artwork in selected tracks";
  }catch(error){if(current())$("#artwork-status").textContent="Could not check shared artwork · Reopen the editor to retry"}
  finally{if(current()&&indexedChanged)refreshTracks().catch(error=>toast(error.message))}
}
async function loadArtwork(path){
  const request=state.artworkRequest;
  try{
    const art=await api(`/api/art?${new URLSearchParams({path})}`);
    if(request!==state.artworkRequest||$("#inspector").classList.contains("hidden"))return;
    $("#cover-preview").innerHTML=`<img alt="Current album art" src="data:${art.mime};base64,${art.data}">`;
    $("#artwork-status").textContent="Embedded artwork found";
    if(state.knownTracks.get(path)&&!state.knownTracks.get(path).artwork)refreshTracks().catch(error=>toast(error.message));
  }catch(error){
    if(request!==state.artworkRequest||$("#inspector").classList.contains("hidden"))return;
    $("#artwork-status").textContent=error.message==="No artwork"?"No embedded artwork":error.message==="Embedded artwork cannot be previewed"?"Embedded artwork exists, but cannot be previewed":"Artwork preview unavailable";
    if(error.message==="No artwork"&&state.knownTracks.get(path)?.artwork)refreshTracks().catch(refreshError=>toast(refreshError.message));
  }
}
async function loadQueuedArtwork(path){const request=state.artworkRequest;try{const art=await api(`/api/queue/art?${new URLSearchParams({path})}`);if(request===state.artworkRequest&&!$("#inspector").classList.contains("hidden")){$("#cover-preview").innerHTML=`<img alt="Queued album art" src="data:${art.mime};base64,${art.data}">`;$("#artwork-status").textContent="Queued replacement artwork"}}catch{if(request===state.artworkRequest)loadArtwork(path)}}
$("#cover-preview").addEventListener("error",event=>{if(event.target.tagName!=="IMG")return;$("#cover-preview").textContent="▣";$("#artwork-status").textContent="Artwork exists, but this image cannot be previewed"},true);
function stopTrackPreview(){const audio=$("#preview-audio");audio.pause();audio.removeAttribute("src");audio.load()}
function closeEditor(){state.artworkRequest++;state.editorFollowsSelection=false;stopTrackPreview();$("#inspector").classList.add("hidden")}
function openModal(eyebrow,title,body,actions){$("#modal-eyebrow").textContent=eyebrow;$("#modal-title").textContent=title;$("#modal-body").innerHTML=body;$("#modal-actions").innerHTML=actions;$("#cancel-all").classList.add("hidden");$("#modal-backdrop").classList.remove("hidden")}
function closeModal(){$("#modal-backdrop").classList.add("hidden");state.confirmation=null;state.jobDetail=null}
function showColumnSettings(){
  openModal("LIBRARY VIEW","Choose columns",`<p>Choose which columns to show and use the arrows to change their order. Track, Disc, Format, and Length have fixed widths.</p><div class="column-options">${columnOrder.map((id,index)=>{
    const column=columns.find(item=>item.id===id);
    return `<div class="column-option" data-column-option="${id}"><label><input type="checkbox" data-column-visible="${id}" ${!hiddenColumns.has(id)||(folderView()&&["title","path"].includes(id))?"checked":""} ${folderView()&&["title","path"].includes(id)?"disabled":""}> ${esc(column.label)}</label><div><button type="button" class="secondary" data-column-move="up" data-column-id="${id}" aria-label="Move ${esc(column.label)} up" ${index===0?"disabled":""}>↑</button><button type="button" class="secondary" data-column-move="down" data-column-id="${id}" aria-label="Move ${esc(column.label)} down" ${index===columnOrder.length-1?"disabled":""}>↓</button></div></div>`;
  }).join("")}</div>`,`<button class="primary" id="modal-cancel">Done</button>`);
}
function updateColumns(){saveColumnSettings();renderColumnHeaders();refreshTracks().catch(error=>toast(error.message));showColumnSettings()}
async function folderPicker(path="/"){try{const data=await api(`/api/browse?${new URLSearchParams({path})}`);const parent=data.path==="/"?"/":data.path.split("/").slice(0,-1).join("/")||"/";openModal("LIBRARY FOLDERS","Choose a folder",`<p>Only folders Cadence can read appear here. In DSM, grant the Cadence system internal user Read/Write access under Control Panel → Shared Folder → Edit → Permissions before browsing.</p><div class="inspector-summary">${esc(data.path)}</div><div class="folder-choices">${data.path!=="/"?`<button class="candidate" data-browse="${esc(parent)}">← Parent folder</button>`:""}${data.folders.map(folder=>`<button class="candidate" data-browse="${esc(folder)}">▣ ${esc(folder.split("/").pop())}</button>`).join("")||"<p>No accessible subfolders. Check DSM shared-folder permissions.</p>"}</div>`,`<button class="secondary" id="modal-cancel">Cancel</button>${data.path!=="/"?`<button class="primary" data-add-root="${esc(data.path)}">Add this folder</button>`:""}`)}catch(error){toast(error.message)}}
function operationLabel(item){return item.operation==="cancel_delete"?"Cancel queued deletion":item.item_kind==="folder"?"Delete empty folder":"Permanently delete file"}
function setupDeletionEditor(queued){
  const folder=state.editKind==="folder",values=queued.map(item=>item?.operation==="delete");
  state.deleteChoice=null;
  $("#metadata-fields").classList.toggle("hidden",folder);$("#organization-fields").classList.toggle("hidden",folder);
  $("#metadata-fields").disabled=false;$("#organization-fields").disabled=false;
  $("#organization-heading").textContent=folder?"Folder deletion":"File & Folder Organization";
  $("#delete-label").textContent=folder?"Delete folders":"Delete files";
  $("#delete-mode").innerHTML=`<option value="keep">Do not delete ${folder?"folder":"file"}(s)</option><option value="delete">Delete ${folder?"folder":"file"}(s)</option>`;
  const mixed=values.some(Boolean)&&!values.every(Boolean);
  $("#delete-mode").selectedIndex=mixed?-1:values.every(Boolean)?1:0;
  $("#delete-help").textContent=mixed?"Mixed queued actions. Choose an option to apply it to every selected item; leaving this unchanged preserves each action.":"Deletion is permanent and happens only after review and commit. Staging deletion replaces other queued changes for these items.";
  syncDeletionControls();
}
function syncDeletionControls(){
  const deleting=$("#delete-mode").value==="delete";
  $("#metadata-fields").disabled=deleting;$("#organization-fields").disabled=deleting;
  if(deleting)stopTrackPreview();
}
$("#delete-mode").addEventListener("change",()=>{state.deleteChoice=$("#delete-mode").value==="delete";syncDeletionControls()});
function pendingChanges(){
  if(state.editKind==="folder"||$("#delete-mode").value==="delete")return {paths:[...state.editing],edits:{},item_kind:state.editKind,delete:state.deleteChoice,revisions:{...state.editRevisions}};
  const edits={...state.draft};delete edits.genre;return {paths:[...state.editing],edits,item_kind:state.editKind,delete:state.deleteChoice,genre_options:genreOptions(),mode:state.modeChanged?$("#organize-mode").value:null,rename:state.renameChanged?$("#rename-mode").value:null,import_tags:$("#import-tags").value,normalize_title:$("#normalize-title").checked,remove_title_text:$("#remove-title-text").value,artwork:state.artwork,revisions:{...state.editRevisions}}}
function hasPendingChange(pending){if(pending.delete!==null&&pending.delete!==undefined)return true;if(pending.item_kind==="folder"||$("#delete-mode").value==="delete")return pending.paths.some(path=>state.queue.has(path));return Object.keys(pending.edits).length||hasGenreChange(pending.genre_options)||pending.mode!==null||pending.rename!==null||pending.import_tags!=="none"||pending.normalize_title||pending.remove_title_text||pending.artwork||pending.paths.every(path=>state.queue.has(path))}
async function addToQueue(){
  try{
    if(!state.editing.length)return;
    const pending=pendingChanges();
    if(!hasPendingChange(pending)){toast("Choose a tag, artwork, folder, or filename change first");return}
    const {items}=await api("/api/queue/add",pending);
    setQueue(items);
    const staged=new Set(items.map(item=>item.path));
    const stagedCount=pending.paths.filter(path=>staged.has(path)).length;
    if(!stagedCount&&pending.delete!==false){toast("No changes needed for the selected items");return}
    closeEditor();
    state.selected.clear();
    await refreshTracks();
    toast(pending.delete===false?"Queue updated":`${stagedCount} item${stagedCount===1?"":"s"} in the queue`);
  }catch(error){toast(error.message)}
}
async function saveChanges(){
  try{
    if(!state.editing.length)return;
    const pending=pendingChanges();
    if(!hasPendingChange(pending)){toast("Choose a change to save first");return}
    state.plan=await api("/api/queue/preview",{pending});
    if(!state.plan.items.length&&!state.plan.errors.length){toast("No changes needed for the selected tracks");return}
    state.previewKind="save";
    state.previewPending=pending;
    state.planPage=0;
    showPlanPage();
  }catch(error){toast(error.message)}
}
async function processQueue(){
  try{
    state.plan=await api("/api/queue/preview",{});
    state.previewKind="queue";
    state.previewPending=null;
    state.planPage=0;
    showPlanPage();
  }catch(error){toast(error.message)}
}
function showPlanPage(){
  state.confirmation=null;
  const plan=state.plan,page=state.planPage,pageSize=50;
  const rows=plan.items.slice(page*pageSize,(page+1)*pageSize).map(item=>{
    const diffs=item.operation&&item.operation!=="edit"?`<div class="warning deletion-summary">${operationLabel(item)}</div>`:fields.filter(field=>!sameTag(field,item.before[field],item.after[field])).map(field=>`<div class="diff-row"><label>${labels[field]}</label><span>${tagMarkup(field,item.before[field])}</span><span>→</span><span class="after">${tagMarkup(field,item.after[field])}</span></div>`).join("")+(item.id3_upgrade?`<div class="warning">Tag format: ID3v2.${item.id3_version_before} → ID3v2.4 for multiple genres. Audio is not re-encoded.</div>`:"");
    return `<div class="diff-item"><div class="diff-item-head"><strong>${esc(item.source.split("/").pop())} ${item.format?`<span class="format-badge">${esc(item.format)}</span>`:""}</strong>${state.previewKind==="queue"?`<button class="text-button remove-queued" data-remove-queued="${esc(item.source)}">Remove from queue</button>`:""}</div><div class="move">From: ${esc(item.source)}</div>${item.destination!==item.source?`<div class="move">To: ${esc(item.destination)}</div>`:""}${diffs}${item.artwork_changed?`<div class="move">Artwork will change</div>`:""}</div>`
  }).join("");
  const warnings=plan.errors.length?`<div class="warning">${plan.errors.length} item(s) cannot be changed. They will be skipped.${plan.errors.map(item=>`<div class="queue-error">${esc(item.path+": "+item.error)} ${state.previewKind==="queue"?`<button class="text-button remove-queued" data-remove-queued="${esc(item.path)}">Remove from queue</button>`:""}</div>`).join("")}</div>`:"";
  const end=Math.min((page+1)*pageSize,plan.items.length);
  openModal("BEFORE YOU COMMIT",`${plan.items.length} proposed change${plan.items.length===1?"":"s"}`,`<p>Review the exact paths and operations. File and folder deletions are permanent. Changes cannot be undone in Cadence.</p>${warnings}${plan.items.length?`<p>Showing ${page*pageSize+1}–${end} of ${plan.items.length}</p>`:""}${rows}`,`<button class="secondary" id="modal-cancel">Continue editing</button>${page?`<button class="secondary" id="preview-previous">← Previous</button>`:""}${end<plan.items.length?`<button class="secondary" id="preview-next">Next →</button>`:""}<button class="primary" id="confirm-commit" ${plan.items.length?"":"disabled"}>Commit to disk →</button>`);
  $("#cancel-all").classList.remove("hidden");
}
function showFinalConfirmation(action){
  state.confirmation=action;
  const committing=action==="commit";
  const queuedCount=state.queue.size;
  const operations=committing?state.plan.items:[];
  const hasDeletes=operations.some(item=>item.operation==="delete"),hasEdits=operations.some(item=>!item.operation||item.operation==="edit");
  openModal("FINAL CONFIRMATION",committing?"Commit these changes to disk?":queuedCount?"Cancel all queued changes?":"Discard this edit?",
    committing?`<p>This schedules one background job for ${state.plan.items.length} item${state.plan.items.length===1?"":"s"}. It will wait behind any active job. Queued items leave the queue immediately; failures return for review. ${hasDeletes?"Selected files and empty folders will be permanently deleted, without a backup or Undo. ":""}${hasEdits?"Tags are written in place without a backup or Undo. An interrupted write can leave a file damaged; keep NAS snapshots or other backups.":!hasDeletes?"This only cancels queued deletion requests. No files or folders will be modified.":""}</p>`:
      queuedCount?`<p>This removes all ${queuedCount} queued change${queuedCount===1?"":"s"} and discards the open edit. No files or folders will be modified.</p>`:"<p>This discards the open edit. No files or folders will be modified.</p>",
    `<button class="secondary" id="back-to-preview">Cancel</button><button class="${committing?"primary":"danger-button"}" id="confirm-final">OK</button>`);
}
async function commitPlan(){
  state.folderScope=null;
  const data=await api("/api/commit",{id:state.plan.id});setQueue(data.queue);closeModal();closeEditor();state.selected.clear();toast(`Commit ${data.state==="queued"?"queued":"started"}: ${data.job.slice(0,8)}`);showView("activity");
}
function jobKindLabel(kind){return kind==="folder_scan"?"Folder scan":kind==="scheduled_scan"?"Scheduled scan":kind}
async function loadActivity(){try{
  const {jobs}=await api("/api/jobs");
  $("#clear-completed-jobs").disabled=!jobs.some(job=>job.state==="complete");
  $("#jobs-list").innerHTML=jobs.length?jobs.map(job=>`<div class="activity-item job-row" data-job-state="${esc(job.state)}"><div class="job-summary"><strong>${esc(jobKindLabel(job.kind))} · ${esc(job.state)}</strong><span>${job.done}${job.total?` / ${job.total}`:""} items · ${new Date(job.created*1000).toLocaleString()}</span>${job.errors.length?`<span>${esc(job.errors.length)} issue(s): ${esc(typeof job.errors[0]==="string"?job.errors[0]:job.errors[0].error)}</span>`:""}</div><div class="job-controls">${["running","queued"].includes(job.state)?`<button class="secondary" data-cancel="${esc(job.id)}">Cancel job</button>`:""}<span class="job-links"><button class="text-button" data-details="${esc(job.id)}">Details</button><button class="text-button danger" data-clear-job="${esc(job.id)}" ${["running","cancelling"].includes(job.state)?'disabled title="Cancel or wait for this job before clearing"':""}>Clear</button></span></div></div>`).join(""):`<p>No jobs yet.</p>`;
}catch(error){toast(error.message)}}
async function showJobDetails(jobId,offset=0){
  try{
    const data=await api(`/api/jobs/detail?${new URLSearchParams({id:jobId,offset,limit:50})}`);
    state.jobDetail={id:jobId,offset};
    const job=data.job;
    const issues=job.errors.length?`<div class="warning"><strong>${job.errors.length} issue${job.errors.length===1?"":"s"}</strong>${job.errors.map(error=>`<div class="queue-error">${esc(typeof error==="string"?error:`${error.path}: ${error.error}`)}</div>`).join("")}</div>`:"";
    const rows=data.items.map(item=>{
      const before=JSON.parse(item.before_tags),after=JSON.parse(item.after_tags);
      const diffs=item.operation&&item.operation!=="edit"?`<div class="warning deletion-summary">${operationLabel(item)}</div>`:fields.filter(field=>!sameTag(field,before[field],after[field])).map(field=>`<div class="diff-row"><label>${labels[field]}</label><span>${tagMarkup(field,before[field])}</span><span>→</span><span class="after">${tagMarkup(field,after[field])}</span></div>`).join("");
      return `<div class="diff-item"><div class="diff-item-head"><strong>${esc(item.source.split("/").pop())}</strong><span class="job-state job-state-${esc(item.state)}">${esc(item.state)}</span></div><div class="move">From: ${esc(item.source)}</div>${item.destination!==item.source?`<div class="move">To: ${esc(item.destination)}</div>`:""}${item.state==="complete"?`<div class="move">Completed: ${esc(formatTimestamp(item.completed_at))}</div>`:""}${diffs}${item.artwork_changed?'<div class="move">Artwork changes</div>':""}${item.error?`<div class="warning">${esc(item.error)}</div>`:""}</div>`;
    }).join("");
    const end=Math.min(offset+data.items.length,data.total_items);
    openModal("JOB DETAILS",`${jobKindLabel(job.kind)} · ${job.state}`,`<p>${job.done}${job.total?` of ${job.total}`:""} items processed · ${data.remaining} edit${data.remaining===1?"":"s"} remaining</p><div class="job-timestamps"><span>Created: ${esc(formatTimestamp(job.created))}</span><span>Started: ${esc(formatTimestamp(job.started_at))}</span><span>Stopped: ${esc(formatTimestamp(job.stopped_at))}</span></div>${issues}${data.total_items?`<p>Showing ${offset+1}–${end} of ${data.total_items}</p>${rows}`:"<p>No per-item changes were recorded for this job.</p>"}`,`<button class="secondary" id="job-detail-refresh">Refresh</button>${offset?'<button class="secondary" id="job-detail-previous">← Previous</button>':""}${end<data.total_items?'<button class="secondary" id="job-detail-next">Next →</button>':""}<button class="primary" id="modal-cancel">Close</button>`);
  }catch(error){toast(error.message)}
}
async function loadSettings(){try{const data=await api("/api/settings");$("#contact-email").value=data.contact_email;$("#confirm-commit-setting").checked=data.confirm_commit!==false;$("#daily-scan-setting").checked=data.daily_scan_enabled===true;state.autoId3Upgrade=data.auto_id3_upgrade!==false;$("#auto-id3-upgrade-setting").checked=state.autoId3Upgrade;syncGenreUpgradeControl(state.editing.map(path=>state.queue.get(path)));$("#acoustid-key").placeholder=data.acoustid_configured?"Key saved · leave blank to keep it":"Enter your application key"}catch(error){toast(error.message)}}
async function match(fingerprint){if(state.editing.length!==1)return;$("#candidate-list").textContent="Searching…";try{const data=await api("/api/match",{path:state.editing[0],fingerprint});$("#candidate-list").innerHTML=data.candidates.length?data.candidates.map((candidate,index)=>`<button class="candidate" data-candidate="${index}"><strong>${esc(candidate.tags.title||"Unknown title")}</strong><small>${esc(candidate.tags.artist)} · ${esc(candidate.tags.album)} · ${Math.round(candidate.confidence*100)}% · ${esc(candidate.source)}</small></button>`).join(""):`<p>No matches found.</p>`;state.candidates=data.candidates}catch(error){$("#candidate-list").textContent="";toast(error.message)}}
async function init(){try{const session=await api("/api/session");if(!session.authenticated){$("#remember-me").checked=false;$("#login").classList.remove("hidden");if(!session.configured)$("#login-error").textContent="Administrator password must be set before first launch.";return}state.csrf=session.csrf;$("#login").classList.add("hidden");$("#app").classList.remove("hidden");setupLibraryTable();await Promise.all([refreshRoots(),loadQueue(),loadSettings()]);await refreshTracks();state.poll=setInterval(()=>{if(state.view==="activity"){loadActivity();loadQueue()}},4000)}catch(error){toast(error.message)}}
let inspectorWidth=440;
try{const saved=Number(localStorage.getItem("cadence-inspector-width"));if(Number.isFinite(saved)&&saved>=320)inspectorWidth=saved}catch{}
function applyInspectorWidth(width=inspectorWidth){
  const max=window.innerWidth,min=Math.min(320,max);
  inspectorWidth=Math.max(min,Math.min(max,width));
  $("#inspector").style.setProperty("--inspector-width",`${inspectorWidth}px`);
  const handle=$("#inspector-resize");
  handle.setAttribute("aria-valuemin",min);
  handle.setAttribute("aria-valuemax",max);
  handle.setAttribute("aria-valuenow",Math.round(inspectorWidth));
}
function saveInspectorWidth(){try{localStorage.setItem("cadence-inspector-width",inspectorWidth)}catch{}}
const inspectorResize=$("#inspector-resize");
inspectorResize.addEventListener("pointerdown",event=>{
  if(event.button!==0)return;
  event.preventDefault();
  const startX=event.clientX,startWidth=$("#inspector").getBoundingClientRect().width,pointerId=event.pointerId;
  inspectorResize.focus();
  inspectorResize.setPointerCapture(pointerId);
  document.body.classList.add("resizing-inspector");
  const move=motion=>{if(motion.pointerId===pointerId)applyInspectorWidth(startWidth+startX-motion.clientX)};
  const finish=motion=>{
    if(motion.pointerId!==pointerId)return;
    inspectorResize.removeEventListener("pointermove",move);
    for(const type of ["pointerup","pointercancel","lostpointercapture"])inspectorResize.removeEventListener(type,finish);
    document.body.classList.remove("resizing-inspector");
    if(inspectorResize.hasPointerCapture(pointerId))inspectorResize.releasePointerCapture(pointerId);
    saveInspectorWidth();
  };
  inspectorResize.addEventListener("pointermove",move);
  for(const type of ["pointerup","pointercancel","lostpointercapture"])inspectorResize.addEventListener(type,finish);
});
inspectorResize.addEventListener("keydown",event=>{
  if(!["ArrowLeft","ArrowRight","Home","End"].includes(event.key))return;
  event.preventDefault();
  applyInspectorWidth(event.key==="Home"?320:event.key==="End"?window.innerWidth:inspectorWidth+(event.key==="ArrowLeft"?16:-16));
  saveInspectorWidth();
});
window.addEventListener("resize",()=>applyInspectorWidth());
applyInspectorWidth();
$("#login-form").addEventListener("submit",async event=>{event.preventDefault();try{await api("/api/login",{password:$("#password").value,remember_me:$("#remember-me").checked});$("#password").value="";await init()}catch(error){$("#login-error").textContent=error.message}});
document.querySelectorAll(".nav-item").forEach(node=>node.addEventListener("click",()=>showView(node.dataset.view)));
document.addEventListener("click",async event=>{const button=event.target.closest(".sort-button");if(!button||button.disabled)return;if(state.sort===button.dataset.sort){state.direction=state.direction==="asc"?"desc":"asc"}else{state.sort=button.dataset.sort;state.direction="asc"}state.offset=0;try{await refreshTracks()}catch(error){toast(error.message)}});
document.addEventListener("pointerdown",event=>{
  const handle=event.target.closest(".column-resize");
  if(!handle||event.button!==0)return;
  event.preventDefault();
  const id=handle.dataset.column,column=columns.find(item=>item.id===id),startX=event.clientX,startWidth=columnWidths[id],pointerId=event.pointerId;
  if(!column||column.fixed)return;
  handle.setPointerCapture(pointerId);
  document.body.classList.add("resizing-columns");
  const move=motion=>{if(motion.pointerId!==pointerId)return;columnWidths[id]=Math.max(column.min,Math.min(2000,startWidth+motion.clientX-startX));applyColumnWidths()};
  const finish=motion=>{if(motion.pointerId!==pointerId)return;handle.removeEventListener("pointermove",move);handle.removeEventListener("pointerup",finish);handle.removeEventListener("pointercancel",finish);document.body.classList.remove("resizing-columns");saveColumnSettings()};
  handle.addEventListener("pointermove",move);
  handle.addEventListener("pointerup",finish);
  handle.addEventListener("pointercancel",finish);
});
document.addEventListener("keydown",event=>{const handle=event.target.closest(".column-resize");if(!handle||!["ArrowLeft","ArrowRight"].includes(event.key))return;event.preventDefault();const id=handle.dataset.column,column=columns.find(item=>item.id===id);if(!column||column.fixed)return;columnWidths[id]=Math.max(column.min,Math.min(2000,columnWidths[id]+(event.key==="ArrowRight"?16:-16)));applyColumnWidths();saveColumnSettings()});
document.addEventListener("keydown",event=>{
  if(event.key!=="Escape")return;
  if(!$("#modal-backdrop").classList.contains("hidden")){event.preventDefault();closeModal();return}
  if(!$("#inspector").classList.contains("hidden")){
    event.preventDefault();
    const expanded=$("#inspector details[open]");
    if(expanded){expanded.open=false;expanded.querySelector("summary").focus()}else closeEditor();
    return;
  }
  if(!$("#toast").classList.contains("hidden")){$("#toast").classList.add("hidden");event.preventDefault()}
});
$("#logout-button").addEventListener("click",async()=>{await api("/api/logout",{});location.reload()});
$("#scan-button").addEventListener("click",async()=>{state.folderScope=null;try{const data=await api("/api/scan",{});toast(data.state==="queued"?"Scan queued":"Scan started");showView("activity")}catch(error){toast(error.message)}});
$("#add-folder").addEventListener("click",()=>folderPicker());$("#empty-add").addEventListener("click",()=>folderPicker());
$("#configure-columns").addEventListener("click",showColumnSettings);
$("#folder-list").addEventListener("click",async event=>{const remove=event.target.closest("[data-remove]");if(remove){event.stopPropagation();if(confirm(`Remove ${remove.dataset.remove} from the index? Files will not be deleted.`)){try{await api("/api/roots/remove",{path:remove.dataset.remove});state.root="";resetFilteredSelection();await Promise.all([refreshRoots(),loadQueue()]);await refreshFilteredTracks()}catch(error){toast(error.message)}}return}const node=event.target.closest("[data-root]");if(node){state.root=node.dataset.root;resetFilteredSelection();$("#root-filter").value=state.root;await refreshRoots();showView("library")}});
$("#root-filter").addEventListener("change",async event=>{state.root=event.target.value;resetFilteredSelection();await refreshRoots();await refreshFilteredTracks()});
$("#track-filter").addEventListener("change",applyLibraryFilter);
$("#genre-filter").addEventListener("change",async event=>{state.genreKey=event.target.value;resetFilteredSelection();try{await refreshTracks()}catch(error){toast(error.message)}});
let searchTimer;$("#search").addEventListener("input",event=>{clearTimeout(searchTimer);state.query=event.target.value.trim();resetFilteredSelection();searchTimer=setTimeout(async()=>{try{await refreshTracks()}catch(error){toast(error.message)}},250)});
for(const id of ["page-size","page-size-top"])$("#"+id).addEventListener("change",async event=>{state.pageSize=Number(event.target.value);state.offset=Math.floor(state.offset/state.pageSize)*state.pageSize;try{await refreshTracks()}catch(error){toast(error.message)}});
for(const id of ["page-jump","page-jump-top"])$("#"+id).addEventListener("change",async event=>{const page=Number(event.target.value);const pageCount=Math.ceil(state.total/state.pageSize);if(!Number.isInteger(page)||page<1||page>pageCount)return;state.offset=(page-1)*state.pageSize;try{await refreshTracks()}catch(error){toast(error.message)}});
for(const id of ["previous-page","previous-page-top"])$("#"+id).addEventListener("click",async()=>{state.offset=Math.max(0,state.offset-state.pageSize);await refreshTracks()});
for(const id of ["next-page","next-page-top"])$("#"+id).addEventListener("click",async()=>{state.offset+=state.pageSize;await refreshTracks()});
document.addEventListener("change",event=>{if(!["select-page","select-page-sticky"].includes(event.target.id))return;if(event.target.checked)addSelection(state.items.map(item=>item.path));else{state.items.forEach(item=>state.selected.delete(item.path));syncSelection()}});
// Prevent browser text highlighting while extending a track selection.
$("#track-rows").addEventListener("mousedown",event=>{if(event.shiftKey&&event.target.closest("tr[data-path]"))event.preventDefault()});
$("#track-rows").addEventListener("click",event=>{
  const row=event.target.closest("tr[data-path]");if(!row)return;
  const path=row.dataset.path,box=event.target.closest(".row-check");
  if(event.shiftKey){selectTrackRange(path);return}
  if(box||state.selected.size){
    const selecting=box?box.checked:!state.selected.has(path);
    if(selecting){if(addSelection([path]))state.selectionAnchor=path}
    else{state.selected.delete(path);syncSelection()}
  }else openEditor([path]);
});
$("#track-rows").addEventListener("error",event=>{if(event.target.matches(".art-thumb img")){event.target.parentElement.textContent="▣";event.target.parentElement.title="Embedded artwork exists, but cannot be previewed"}},true);
$("#select-list").addEventListener("click",selectEntireList);$("#edit-selected").addEventListener("click",()=>openEditor([...state.selected],true));$("#close-inspector").addEventListener("click",closeEditor);
$("#edit-fields").addEventListener("input",event=>{const field=event.target.dataset.field;if(!field)return;state.touchedFields.add(field);state.draft[field]=event.target.value});
$("#edit-fields").addEventListener("change",event=>{
  const field=event.target.dataset.clear;
  if(field){
    state.touchedFields.add(field);
    const input=$(`#field-${field}`);
    input.disabled=event.target.checked;input.value="";
    if(event.target.checked)state.draft[field]="";else delete state.draft[field];
    return;
  }
  const input=event.target,inputField=input.dataset.field;
  if(state.editing.length>1&&inputField&&state.touchedFields.has(inputField)&&input.value===""){
    input.closest(".field").querySelector(`[data-clear="${inputField}"]`).checked=true;
    input.disabled=true;
  }
});
$("#organize-mode").addEventListener("change",()=>state.modeChanged=true);
$("#rename-mode").addEventListener("change",()=>state.renameChanged=true);
$("#artwork-input").addEventListener("change",event=>{const file=event.target.files[0];if(!file)return;if(!["image/jpeg","image/png"].includes(file.type)||file.size>10*1024*1024){toast("Choose a JPEG or PNG under 10 MB");return}const request=++state.artworkRequest;const reader=new FileReader();reader.onload=()=>{if(request!==state.artworkRequest||$("#inspector").classList.contains("hidden"))return;state.artwork={mime:file.type,data:reader.result.split(",")[1]};$("#cover-preview").innerHTML=`<img alt="New album art" src="${reader.result}">`;$("#artwork-status").textContent="New replacement artwork"};reader.readAsDataURL(file)});
$("#fetch-artwork").addEventListener("click",async()=>{
  const url=$("#artwork-url").value.trim();
  if(!url){toast("Enter an artwork URL first");return}
  const request=++state.artworkRequest;
  const editing=state.editing;
  const button=$("#fetch-artwork");
  button.disabled=true;
  button.textContent="Fetching…";
  button.dataset.request=String(request);
  try{
    const art=await api("/api/art/url",{url});
    if(request!==state.artworkRequest||state.editing!==editing||$("#inspector").classList.contains("hidden"))return;
    state.artwork=art;
    $("#cover-preview").innerHTML=`<img alt="New album art" src="data:${art.mime};base64,${art.data}">`;
    $("#artwork-status").textContent="New replacement artwork";
    toast("Artwork ready for review");
  }catch(error){if(request===state.artworkRequest)toast(error.message)}
  finally{if(button.dataset.request===String(request)){button.disabled=false;button.textContent="Use URL"}}
});
$("#artwork-url").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();$("#fetch-artwork").click()}});
$("#match-text").addEventListener("click",()=>match(false));$("#match-fingerprint").addEventListener("click",()=>match(true));
$("#candidate-list").addEventListener("click",async event=>{const node=event.target.closest("[data-candidate]");if(!node)return;const candidate=state.candidates[Number(node.dataset.candidate)];for(const [field,value] of Object.entries(candidate.tags)){if(value){state.draft[field]=value;const input=$(`#field-${field}`);if(input)input.value=value}}if(candidate.release_id){const request=++state.artworkRequest;try{const art=await api("/api/cover",{release_id:candidate.release_id});if(request===state.artworkRequest&&!$("#inspector").classList.contains("hidden")){state.artwork=art;$("#cover-preview").innerHTML=`<img alt="Suggested album art" src="data:${art.mime};base64,${art.data}">`;$("#artwork-status").textContent="Suggested replacement artwork"}}catch{}}toast("Suggestion staged for review")});
$("#add-to-queue").addEventListener("click",addToQueue);$("#save-changes").addEventListener("click",saveChanges);$("#process-queue").addEventListener("click",processQueue);$("#modal-close").addEventListener("click",closeModal);$("#modal-backdrop").addEventListener("click",event=>{if(event.target.id==="modal-backdrop")closeModal()});
$("#cancel-all").addEventListener("click",()=>showFinalConfirmation("clear"));
$("#modal-backdrop").addEventListener("click",async event=>{
  const moveColumn=event.target.closest("[data-column-move]");
  if(moveColumn){const index=columnOrder.indexOf(moveColumn.dataset.columnId),other=index+(moveColumn.dataset.columnMove==="up"?-1:1);if(index>=0&&other>=0&&other<columnOrder.length){[columnOrder[index],columnOrder[other]]=[columnOrder[other],columnOrder[index]];updateColumns()}return}
  const browse=event.target.closest("[data-browse]");if(browse)return folderPicker(browse.dataset.browse);
  const add=event.target.closest("[data-add-root]");
  if(add){try{const data=await api("/api/roots",{path:add.dataset.addRoot});closeModal();await refreshRoots();toast(`Folder added · scan ${data.state==="running"?"started":data.state}`);showView("activity")}catch(error){toast(error.message)}return}
  const remove=event.target.closest("[data-remove-queued]");
  if(remove){try{const {items}=await api("/api/queue/remove",{path:remove.dataset.removeQueued});setQueue(items);await refreshTracks();if(items.length)await processQueue();else{closeModal();toast("Queue is empty")}}catch(error){toast(error.message)}return}
  if(event.target.id==="modal-cancel"){closeModal();return}
  if(event.target.id==="preview-previous"){state.planPage--;showPlanPage();return}
  if(event.target.id==="preview-next"){state.planPage++;showPlanPage();return}
  if(event.target.id==="confirm-commit"){
    const button=event.target,plan=state.plan;
    button.disabled=true;
    try{
      const settings=await api("/api/settings");
      if(state.plan!==plan||!button.isConnected||$("#modal-backdrop").classList.contains("hidden"))return;
      if(settings.confirm_commit!==false)showFinalConfirmation("commit");else await commitPlan();
    }catch(error){toast(error.message)}
    finally{button.disabled=false}
    return;
  }
  if(event.target.id==="back-to-preview"){showPlanPage();return}
  if(event.target.id==="confirm-final"){
    event.target.disabled=true;
    try{
      if(state.confirmation==="clear"){
        const {items}=await api("/api/queue/clear",{});setQueue(items);closeModal();closeEditor();state.draft={};state.artwork=null;await refreshTracks();toast("Staged changes discarded");
      }else if(state.confirmation==="commit"){
        await commitPlan();
      }
    }catch(error){event.target.disabled=false;toast(error.message)}
  }
});
$("#modal-backdrop").addEventListener("change",event=>{
  const box=event.target.closest("[data-column-visible]");
  if(!box)return;
  if(!box.checked&&hiddenColumns.size===columns.length-1){box.checked=true;toast("Keep at least one track column visible");return}
  if(box.checked)hiddenColumns.delete(box.dataset.columnVisible);else hiddenColumns.add(box.dataset.columnVisible);
  updateColumns();
});
$("#settings-form").addEventListener("submit",async event=>{event.preventDefault();try{const body={contact_email:$("#contact-email").value,confirm_commit:$("#confirm-commit-setting").checked,auto_id3_upgrade:$("#auto-id3-upgrade-setting").checked,daily_scan_enabled:$("#daily-scan-setting").checked};if($("#acoustid-key").value)body.acoustid_key=$("#acoustid-key").value;await api("/api/settings",body);state.autoId3Upgrade=body.auto_id3_upgrade;syncGenreUpgradeControl(state.editing.map(path=>state.queue.get(path)));$("#acoustid-key").value="";toast("Settings saved")}catch(error){toast(error.message)}});
$("#password-form").addEventListener("submit",async event=>{event.preventDefault();const current=$("#current-password").value,replacement=$("#new-password").value;if(replacement!==$("#repeat-password").value){toast("New passwords do not match");return}try{await api("/api/password",{current_password:current,new_password:replacement});$("#password-form").reset();state.csrf="";location.reload()}catch(error){toast(error.message)}});
$("#clear-completed-jobs").addEventListener("click",async()=>{try{const data=await api("/api/jobs/clear-completed",{});await loadActivity();toast(`${data.cleared} completed job${data.cleared===1?"":"s"} cleared`)}catch(error){toast(error.message)}});
$("#jobs-list").addEventListener("click",async event=>{
  const details=event.target.closest("[data-details]");if(details){await showJobDetails(details.dataset.details);return}
  const clear=event.target.closest("[data-clear-job]");if(clear){if(clear.closest("[data-job-state]")?.dataset.jobState==="queued"&&!confirm("Remove this waiting job? It will not run. Queued edits return to the queue, but unstaged Save Changes edits will be discarded."))return;try{await api("/api/jobs/clear",{id:clear.dataset.clearJob});await Promise.all([loadActivity(),loadQueue()]);toast("Job cleared")}catch(error){toast(error.message)}return}
  const cancel=event.target.closest("[data-cancel]");if(cancel){if(cancel.closest("[data-job-state]")?.dataset.jobState==="queued"&&!confirm("Cancel this waiting job? Queued edits return to the queue, but unstaged Save Changes edits will be discarded."))return;try{await api("/api/cancel",{id:cancel.dataset.cancel});await loadActivity();await loadQueue();toast("Cancellation requested")}catch(error){toast(error.message)}}
});
$("#modal-backdrop").addEventListener("click",event=>{if(!state.jobDetail)return;const {id,offset}=state.jobDetail;if(event.target.id==="job-detail-refresh")showJobDetails(id,offset);if(event.target.id==="job-detail-previous")showJobDetails(id,Math.max(0,offset-50));if(event.target.id==="job-detail-next")showJobDetails(id,offset+50)});
init();
