// Run with: node --test tests/test_library_ui.cjs
const {test}=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const path=require("node:path");

const source=fs.readFileSync(path.join(__dirname,"../web/app.js"),"utf8");
const browsing=source.slice(0,source.indexOf("function openEditor("));
function app(){
  const elements=new Map(),storage=new Map();
  const context=vm.createContext({URLSearchParams,console,setTimeout,clearTimeout,requestAnimationFrame:()=>{},
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    document:{querySelector:selector=>{
      if(!elements.has(selector))elements.set(selector,{value:"",textContent:"",selectedOptions:[{textContent:"All tags"}],
        classList:{toggle(){},add(){},remove(){},contains(){return false}},setAttribute(){}});
      return elements.get(selector);
    },querySelectorAll:()=>[]}});
  vm.runInContext(browsing+`\n
    var messages=[],opened=[];
    syncSelection=()=>{};
    toast=message=>messages.push(message);
    closeEditor=()=>{state.editorOpenRequest=(state.editorOpenRequest||0)+1};
    openEditor=(paths,follow)=>opened.push({paths,follow});
    renderColumnHeaders=()=>{};
    refreshFilteredTracks=async()=>{};
    state.libraryView="genres";
  `,context);
  return {context,elements,storage,run:code=>vm.runInContext(code,context)};
}
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done});return {promise,resolve}}

test("overlapping groups deduplicate paths and deselection retains remaining members",async()=>{
  const a=app();
  a.context.fetch=async(_url,options)=>{
    const {keys}=JSON.parse(options.body);
    return {ok:true,json:async()=>({keys,paths:[...new Set(keys.flatMap(key=>key==="rock"?["a","b"]:["a"]))]})};
  };
  await a.run('addSelection(["rock","folk"])');
  assert.equal(a.run('state.selected.size'),2);
  assert.equal(a.run('state.groupPaths.join(",")'),"a,b");
  await a.run('removeSelection(["rock"])');
  assert.equal(a.run('state.groupPaths.join(",")'),"a");
  await a.run('removeSelection(["folk"])');
  assert.equal(a.run('state.groupPaths.length'),0);
});

test("expanded selection limit failure preserves previous selection",async()=>{
  const a=app();
  a.run('state.selected=new Set(["folk"]);state.groupPaths=["a"]');
  a.context.fetch=async()=>({ok:false,json:async()=>({error:"5,000-track batch limit"})});
  assert.equal(await a.run('addSelection(["huge"])'),false);
  assert.equal(a.run('[...state.selected].join(",")'),"folk");
  assert.equal(a.run('state.groupPaths.join(",")'),"a");
  assert.equal(a.run('state.resolvingSelection'),false);
  assert.match(a.run('messages[0]'),/5,000/);
});

test("selection responses cannot cross a filter or view change",async()=>{
  const a=app(),pending=deferred();
  a.context.fetch=()=>pending.promise;
  const selecting=a.run('addSelection(["rock"])');
  assert.equal(a.run('state.resolvingSelection'),true);
  a.run('resetFilteredSelection();state.libraryView="albums"');
  pending.resolve({ok:true,json:async()=>({keys:["rock"],paths:["a","b"]})});
  await selecting;
  assert.equal(a.run('state.selected.size'),0);
  assert.equal(a.run('state.groupPaths.length'),0);
});

test("select entire list uses group keys and unique underlying track count",async()=>{
  const a=app();let calls=0;
  a.run('state.total=2;state.totalTracks=2;state.items=[{key:"rock"},{key:"folk"}]');
  a.context.fetch=async(_url,options)=>{
    calls++;assert.equal(JSON.parse(options.body).all_matching,true);
    return {ok:true,json:async()=>({keys:["rock","folk"],paths:["a","b"]})};
  };
  await a.run('selectEntireList()');
  assert.equal(a.run('state.selected.size'),2);
  assert.equal(a.run('state.groupPaths.length'),2);
  await a.run('selectEntireList()');
  assert.equal(a.run('state.selected.size'),0);
  a.run('state.total=1;state.totalTracks=5001');
  await a.run('selectEntireList()');
  assert.equal(calls,2);
});

test("Shift selection uses displayed group order and retains existing selection",async()=>{
  const a=app();
  a.run('state.items=[{key:"a"},{key:"b"},{key:"c"}];state.selected=new Set(["outside","a"]);state.selectionAnchor="a"');
  a.context.fetch=async(_url,options)=>{
    const {keys}=JSON.parse(options.body);
    return {ok:true,json:async()=>({keys,paths:keys.map(key=>key+".mp3")})};
  };
  await a.run('selectTrackRange("c")');
  assert.equal(a.run('[...state.selected].sort().join(",")'),"a,b,c,outside");
});

test("switching views retains query and filters but resets selection and restores columns",async()=>{
  const a=app();
  a.run('state.libraryView="tracks";loadColumnSettings();hiddenColumns.add("path");state.sort="artist";saveColumnSettings();state.query="Blue";state.trackFilter="tag_search";state.tagField="title";state.selected.add("a")');
  a.run('$("#library-view-select").value="albums"');
  await a.run('changeLibraryView()');
  assert.equal(a.run('state.query'),"Blue");
  assert.equal(a.run('state.tagField'),"title");
  assert.equal(a.run('state.trackFilter'),"tag_search");
  assert.equal(a.run('state.selected.size'),0);
  assert.equal(a.run('columns[0].label'),"Album");
  a.run('hiddenColumns.add("year");state.sort="tracks";$("#library-view-select").value="tracks"');
  await a.run('changeLibraryView()');
  assert.equal(a.run('hiddenColumns.has("path")'),true);
  assert.equal(a.run('hiddenColumns.has("year")'),false);
  assert.equal(a.run('state.sort'),"artist");
  a.run('state.trackFilter="empty_folders";$("#library-view-select").value="albums"');
  await a.run('changeLibraryView()');
  assert.equal(a.run('state.trackFilter'),"all");
  assert.equal(a.run('hiddenColumns.has("year")'),true);
  assert.equal(a.run('state.sort'),"tracks");
});

test("closing an editor while a group resolves prevents it reopening",async()=>{
  const a=app(),pending=deferred();
  a.context.fetch=()=>pending.promise;
  const opening=a.run('openGroupEditor(["rock"])');
  a.run('closeEditor()');
  pending.resolve({ok:true,json:async()=>({keys:["rock"],paths:["a","b"]})});
  await opening;
  assert.equal(a.run('opened.length'),0);
});

test("a one-track group supplies the existing single-track editor its file record",async()=>{
  const a=app();
  a.context.fetch=async()=>({ok:true,json:async()=>({keys:["folk"],paths:["a"],track:{path:"a",tags:{title:"Song"}}})});
  await a.run('openGroupEditor(["folk"])');
  assert.equal(a.run('state.knownTracks.get("a").tags.title'),"Song");
  assert.equal(a.run('opened[0].paths.join(",")'),"a");
});
