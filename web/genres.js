function genreValues(value){return Array.isArray(value)?[...value]:value?[value]:[]}
function sameTag(field,first,second){return field==="genre"?JSON.stringify(genreValues(first))===JSON.stringify(genreValues(second)):first===second}
function tagMarkup(field,value){return field==="genre"?genreValues(value).map(genre=>`<span class="genre-label">${esc(genre||"(empty)")}</span>`).join(" ")||"—":esc(value||"—")}
function genreEditorMarkup(){
  return `<section class="genre-editor field full" aria-label="Genre editor"><div class="field-heading"><label for="genre-input">Genres</label><span id="genre-status" role="status">Loading stored genres…</span></div><fieldset id="genre-controls" disabled><div id="genre-chips" class="genre-chips" aria-label="Selected genres"></div><div class="genre-entry"><input id="genre-input" type="text" maxlength="1000" list="genre-suggestions" placeholder="Choose or create a genre" autocomplete="off" aria-describedby="genre-help"><datalist id="genre-suggestions"></datalist><button type="button" class="secondary" id="genre-add">Add</button></div><p id="genre-help">The displayed genres replace the selected tracks' genres when changed. Enter or Add creates one chip; click a chip to edit it. Commas remain literal unless you choose Split below.</p><label class="genre-action" for="genre-action">Genre action<select id="genre-action"><option value="replace">Overwrite tracks with selected genre(s)</option><option value="add">Add selected genre(s) to each track</option><option value="remove">Remove selected genre(s) from each track</option></select></label><details class="genre-options"><summary>Genre cleanup &amp; compatibility</summary><label><input id="genre-clean" type="checkbox">Trim whitespace and remove duplicate genres</label><label><input id="genre-split" type="checkbox">Split comma/semicolon lists into separate genres</label><label><input id="genre-upgrade" type="checkbox">Allow ID3v2.4 upgrade when multiple genres require it</label><p>Cleanup applies separately to each track. Splitting also trims and deduplicates; slashes stay intact. ID3 upgrades appear in the disk-write review. Player support for multiple genres varies.</p></details></fieldset></section>`;
}
function setEditorGenres(values,mixed=false){
  state.genreDrafts.replace=genreValues(values);
  state.genreValues=state.genreDrafts.replace;
  state.genreOriginal=[...state.genreValues];
  state.genreMixed=mixed;
  state.genreEditing=null;
  $("#genre-controls").disabled=false;
  updateGenreStatus();
  renderGenreChips();
}
function updateGenreStatus(){
  const action=$("#genre-action").value;
  if(action==="add"||action==="remove"){
    $("#genre-status").textContent=state.genreValues.length?`${state.genreValues.length} genre${state.genreValues.length===1?"":"s"} to ${action}`:`Choose genres to ${action}`;
    $("#genre-help").textContent=action==="add"?"These genres are added to each track. Genres already present are left alone. Enter or Add creates one chip; click a chip to edit it.":"These genres are removed from each track. Genres that are absent are left alone. Enter or Add creates one chip; click a chip to edit it.";
  }else{
    $("#genre-status").textContent=state.touchedFields.has("genre")?"Replacement genres":state.genreMixed?"Mixed genres · preserved unless changed":state.genreValues.length?`${state.genreValues.length} genre${state.genreValues.length===1?"":"s"}`:"No genres";
    $("#genre-help").textContent="The displayed genres replace the selected tracks' genres when changed. Enter or Add creates one chip; click a chip to edit it. Commas remain literal unless you choose Split below.";
  }
}
function renderGenreChips(){
  $("#genre-chips").innerHTML=state.genreValues.map((genre,index)=>`<span class="genre-chip"><button type="button" class="genre-name" data-edit-genre="${index}" aria-label="Edit genre ${esc(genre||"(empty)")}">${esc(genre||"(empty)")}</button><button type="button" class="genre-remove" data-remove-genre="${index}" aria-label="Remove genre ${esc(genre||"(empty)")}">×</button></span>`).join("");
}
function markGenreChange(){
  if($("#genre-action").value==="replace")state.touchedFields.add("genre");
  updateGenreStatus();
}
function addGenreChip(){
  const input=$("#genre-input"),value=input.value.trim();
  if(!value)return;
  const duplicate=state.genreValues.findIndex((genre,index)=>index!==state.genreEditing&&genre.trim().toLowerCase()===value.toLowerCase());
  if(duplicate>=0){toast("That genre is already selected");return}
  if(state.genreValues.length>=100&&state.genreEditing===null){toast("Use up to 100 genres");return}
  markGenreChange();
  if(state.genreEditing===null)state.genreValues.push(value);else state.genreValues[state.genreEditing]=value;
  state.genreEditing=null;
  input.value="";
  $("#genre-add").textContent="Add";
  renderGenreChips();
  updateGenreStatus();
  input.focus();
}
function genreOptions(){
  if($("#genre-input").value.trim())throw new Error("Press Add to finish entering the genre, or clear the genre input first");
  const action=$("#genre-action").value;
  const mode=action==="replace"?(state.touchedFields.has("genre")?"replace":"keep"):(state.genreValues.length?action:"keep");
  return {mode,values:mode==="keep"?[]:[...state.genreValues],clean:$("#genre-clean").checked,split:$("#genre-split").checked,...(state.autoId3Upgrade?{}:{upgrade:$("#genre-upgrade").checked})};
}
function hasGenreChange(options){return options.mode!=="keep"||options.clean||options.split}
function syncGenreUpgradeControl(queued=[]){
  const box=$("#genre-upgrade");
  if(!box)return;
  const wasAutomatic=box.disabled;
  box.disabled=state.autoId3Upgrade;
  if(state.autoId3Upgrade)box.checked=true;
  else if(wasAutomatic)box.checked=queued.some(item=>item?.genre_upgrade);
  box.closest("label").classList.toggle("disabled-option",state.autoId3Upgrade);
}
function setupGenreEditor(queued){
  state.genreDrafts={replace:[],add:[],remove:[]};state.genreValues=state.genreDrafts.replace;state.genreOriginal=[];state.genreMixed=false;state.genreEditing=null;
  $("#genre-upgrade").checked=queued.some(item=>item?.genre_upgrade);
  syncGenreUpgradeControl(queued);
  $("#genre-add").addEventListener("click",addGenreChip);
  $("#genre-action").addEventListener("change",()=>{
    state.genreValues=state.genreDrafts[$("#genre-action").value];
    state.genreEditing=null;
    $("#genre-input").value="";
    $("#genre-add").textContent="Add";
    renderGenreChips();
    updateGenreStatus();
  });
  $("#genre-chips").addEventListener("click",event=>{
    const remove=event.target.closest("[data-remove-genre]"),edit=event.target.closest("[data-edit-genre]");
    if(remove){markGenreChange();state.genreValues.splice(Number(remove.dataset.removeGenre),1);state.genreEditing=null;$("#genre-input").value="";$("#genre-add").textContent="Add";renderGenreChips();updateGenreStatus();$("#genre-input").focus()}
    if(edit){state.genreEditing=Number(edit.dataset.editGenre);$("#genre-input").value=state.genreValues[state.genreEditing];$("#genre-add").textContent="Update";$("#genre-input").focus()}
  });
  $("#genre-input").addEventListener("keydown",event=>{
    if(event.key==="Enter"){event.preventDefault();addGenreChip()}
    if(event.key==="Escape"&&event.target.value){event.preventDefault();event.stopPropagation();event.target.value="";state.genreEditing=null;$("#genre-add").textContent="Add"}
  });
  let timer,request=0;
  const paths=state.editing;
  async function suggestions(){
    const revision=++request,query=$("#genre-input").value;
    try{const data=await api(`/api/genres?${new URLSearchParams({q:query})}`);if(state.editing===paths&&revision===request&&query===$("#genre-input").value)$("#genre-suggestions").innerHTML=data.genres.map(genre=>`<option value="${esc(genre)}"></option>`).join("")}catch{}
  }
  $("#genre-input").addEventListener("input",()=>{clearTimeout(timer);timer=setTimeout(suggestions,200)});
  $("#genre-input").addEventListener("focus",suggestions);
  suggestions();
}
