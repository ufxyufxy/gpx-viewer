/* GPX / FIT 快速解析内核
   - GPX 用字符串扫描代替 DOMParser，避免每个点做子树查询
   - 时间统一换算成 epoch 毫秒，按「本地日」缓存边界，点级只有数值比较
   - FIT 是二进制顺序读取，只取 record(20) 里的 position_lat / position_long / timestamp
   - 输出结构与旧版 parseGpx 完全一致，两个界面模板通用
   解析与解压是纯计算；末尾的快照导出会读一次当前文档的 DOM，用来把数据固化进 HTML 文件。 */
(function(){
"use strict";

var pad2 = function(n){ return String(n).padStart(2,"0"); };
var FIT_EPOCH = 631065600;              // 1989-12-31T00:00:00Z，FIT 时间戳纪元（秒）
var SEMI = 180 / 2147483648;            // semicircle -> degree
var MIN_MS = 946684800000;              // 2000-01-01，更早的时间当作无效
var MAX_MS = Date.now() + 366*86400000; // 明年之后也当作无效
var INVALID_S32 = 0x7FFFFFFF;
var TD = new TextDecoder("utf-8");

function haversine(a,b){
  var R=6371000, rad=Math.PI/180;
  var dLat=(b[0]-a[0])*rad, dLon=(b[1]-a[1])*rad;
  var s=Math.sin(dLat/2)*Math.sin(dLat/2) + Math.cos(a[0]*rad)*Math.cos(b[0]*rad)*Math.sin(dLon/2)*Math.sin(dLon/2);
  return 2*R*Math.asin(Math.sqrt(s));
}

/* 本地日期解析：同一天内只算一次 Date，点级只有两次比较 */
function dayResolver(){
  var key=null, start=-Infinity, end=-Infinity;
  return function(ms){
    if(ms>=start && ms<end) return key;
    var d=new Date(ms);
    var y=d.getFullYear(), m=d.getMonth(), day=d.getDate();
    key=y+"-"+pad2(m+1)+"-"+pad2(day);
    start=new Date(y,m,day).getTime();
    end=new Date(y,m,day+1).getTime();
    return key;
  };
}
function hhmmMs(ms){
  if(!ms) return "";
  var d=new Date(ms);
  return pad2(d.getHours())+":"+pad2(d.getMinutes());
}
function parseDateKey(raw){
  var s=String(raw||"");
  var m=s.match(/(20\d{2})[-_.\/\s年](\d{1,2})[-_.\/\s月](\d{1,2})/);
  if(!m) m=s.match(/(20\d{2})(\d{2})(\d{2})/);
  if(!m) return null;
  var y=+m[1], mo=+m[2], d=+m[3];
  if(mo<1||mo>12||d<1||d>31) return null;
  return y+"-"+pad2(mo)+"-"+pad2(d);
}

/* ISO8601 -> epoch ms。手写数字切片，比 new Date(iso) 快得多。
   支持 ...Z / ...+08:00 / 无时区（按本地时间）/ 只有日期。 */
function numAt(s,a,b){
  var v=0;
  for(var i=a;i<b;i++){
    var c=s.charCodeAt(i)-48;
    if(c<0||c>9) return -1;
    v=v*10+c;
  }
  return v;
}
function isoMs(s){
  if(!s || s.length<10) return 0;
  var y=numAt(s,0,4), mo=numAt(s,5,7), d=numAt(s,8,10);
  if(y<=0||mo<=0||d<=0) return 0;
  var h=s.length>=13?numAt(s,11,13):0, mi=s.length>=16?numAt(s,14,16):0, se=s.length>=19?numAt(s,17,19):0;
  if(h<0) h=0; if(mi<0) mi=0; if(se<0) se=0;
  var ms=Date.UTC(y,mo-1,d,h,mi,se);
  var i=19;
  if(s.charCodeAt(i)===46){                 // .123 毫秒
    i++;
    while(i<s.length){ var c=s.charCodeAt(i); if(c<48||c>57) break; i++; }
  }
  var z=s.charCodeAt(i);
  if(z===90||z===122){                      // Z，UTC
  }else if(z===43||z===45){                 // +08:00 / -05:00
    var oh=numAt(s,i+1,i+3), om=numAt(s,i+4,i+6);
    if(oh>=0&&om>=0) ms-=(z===43?1:-1)*(oh*60+om)*60000;
  }else if(!isNaN(z)){                      // 别的尾巴，按 UTC
  }else{
    ms=new Date(y,mo-1,d,h,mi,se).getTime();   // 无时区，按本地时间解释
  }
  if(ms<MIN_MS || ms>MAX_MS) return 0;         // 1970 之类的假时间
  return ms;
}

function attrVal(s,name){
  var p=s.indexOf(name+"=");
  if(p<0) return null;
  var q=p+name.length+1, qc=s.charCodeAt(q);
  if(qc!==34 && qc!==39) return null;
  q++;
  var e=s.indexOf(String.fromCharCode(qc),q);
  if(e<0) return null;
  var v=parseFloat(s.slice(q,e));
  return isFinite(v)?v:null;
}

/* 扫描一段 XML 里的 trkpt / rtept，返回 [[lat,lon,ms], ...] */
function scanPoints(body, tag){
  var pts=[], open="<"+tag, close="</"+tag+">";
  var i=0, n=body.length, olen=open.length;
  while(true){
    var p=body.indexOf(open,i);
    if(p<0) break;
    var c=body.charCodeAt(p+olen);
    if(!(c===32||c===62||c===47||c===9||c===10||c===13)){ i=p+olen; continue; }
    var gt=body.indexOf(">",p);
    if(gt<0) break;
    var attrs=body.slice(p+olen,gt);
    var lat=attrVal(attrs,"lat"), lon=attrVal(attrs,"lon");
    var ms=0, next, selfClose=false, al=attrs.length-1;
    while(al>=0){
      var cc=attrs.charCodeAt(al);
      if(cc===32||cc===9||cc===10||cc===13){ al--; continue; }
      selfClose=(cc===47); break;
    }
    if(selfClose){                              // 自闭合 <trkpt .../>
      next=gt+1;
    }else{
      var e=body.indexOf(close,gt);
      var stop=e<0?n:e;
      var t=body.indexOf("<time",gt+1);
      if(t>=0 && t<stop){
        var t1=body.indexOf(">",t), t2=t1<0?-1:body.indexOf("</time>",t1+1);
        if(t1>=0&&t2>=0) ms=isoMs(body.slice(t1+1,t2).trim());
      }
      next=e<0?n:e+close.length;
    }
    if(lat!==null && lon!==null) pts.push([lat,lon,ms]);
    i=next;
  }
  return pts;
}
function scanSegments(text){
  var segs=[], defaultName="";
  var nm=/<name[^>]*>\s*([^<]*?)\s*<\/name>/i.exec(text);
  if(nm) defaultName=nm[1];
  var re=/<trkseg\b[^>]*>([\s\S]*?)<\/trkseg\s*>/gi, m;
  while((m=re.exec(text))!==null){
    var pts=scanPoints(m[1],"trkpt");
    if(pts.length) segs.push({name:defaultName,pts:pts});
  }
  if(!segs.length){
    var re2=/<(trk|rte)\b[^>]*>([\s\S]*?)<\/\1\s*>/gi;
    while((m=re2.exec(text))!==null){
      var p2=scanPoints(m[2], m[1].toLowerCase()==="rte"?"rtept":"trkpt");
      if(p2.length) segs.push({name:defaultName,pts:p2});
    }
  }
  if(!segs.length){
    var p3=scanPoints(text,"trkpt");
    if(!p3.length) p3=scanPoints(text,"rtept");
    if(p3.length) segs.push({name:defaultName,pts:p3});
  }
  return segs;
}

/* 按每个点自带的时间切天；新段带前一点，跨零点的线不会断 */
function splitByDayMs(pts, resolver){
  var n=pts.length, keys=new Array(n), i;
  for(i=0;i<n;i++) keys[i]=pts[i][2] ? resolver(pts[i][2]) : null;
  for(i=1;i<n;i++) if(!keys[i]) keys[i]=keys[i-1];
  for(i=n-2;i>=0;i--) if(!keys[i]) keys[i]=keys[i+1];
  var out=[], cur=null;
  for(i=0;i<n;i++){
    var p=pts[i], k=keys[i];
    if(!cur || cur.key!==k){
      cur={key:k, pts:[[p[0],p[1]]], lead:(i>0?[pts[i-1][0],pts[i-1][1]]:null), t0:p[2], t1:p[2]};
      out.push(cur);
    }else{
      cur.pts.push([p[0],p[1]]);
      if(p[2]){
        if(!cur.t0 || p[2]<cur.t0) cur.t0=p[2];
        if(!cur.t1 || p[2]>cur.t1) cur.t1=p[2];
      }
    }
  }
  return out;
}
function buildPieces(segs, fileName){
  var resolver=dayResolver(), buckets=new Map();
  segs.forEach(function(rs){
    splitByDayMs(rs.pts,resolver).forEach(function(part){
      var k=part.key||"";
      if(!buckets.has(k)) buckets.set(k,{segs:[],times:[],name:""});
      var b=buckets.get(k);
      if(part.pts.length) b.segs.push(part.lead?{pts:[part.lead].concat(part.pts),skip:1}:{pts:part.pts,skip:0});
      if(part.t0) b.times.push(part.t0);
      if(part.t1) b.times.push(part.t1);
      if(!b.name && rs.name) b.name=rs.name;
    });
  });
  var keyFile=parseDateKey(fileName), baseName=fileName.replace(/\.(gpx|fit)$/i,""), pieces=[];
  buckets.forEach(function(b,k){
    var dist=0, n=0;
    b.segs.forEach(function(sg){
      var p=sg.pts;
      n+=p.length-(sg.skip?1:0);
      for(var i=1;i<p.length;i++) dist+=haversine(p[i-1],p[i]);
    });
    var ts=b.times.slice().sort(function(a,b){return a-b;});
    pieces.push({
      keyTime:k||null, keyFile:keyFile, file:fileName,
      name:b.name||baseName, segs:b.segs.map(function(sg){return sg.pts;}), dist:dist, pts:n,
      start:ts.length?hhmmMs(ts[0]):"", end:ts.length?hhmmMs(ts[ts.length-1]):"",
      t0:ts.length?ts[0]:0
    });
  });
  pieces.sort(function(a,b){return (a.t0||Infinity)-(b.t0||Infinity);});
  var firstDay=pieces.length?pieces[0].keyTime:null;
  pieces.forEach(function(it,i){ it.part=i+1; it.parts=pieces.length; it.keyStart=firstDay; });
  return pieces.length?pieces:null;
}

function viewOf(bytes){
  if(bytes instanceof ArrayBuffer) return new DataView(bytes);
  if(ArrayBuffer.isView(bytes)) return new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
  return new DataView(new Uint8Array(bytes).buffer);
}
function decode(bytes){
  if(typeof bytes==="string") return bytes;
  var u8=bytes instanceof Uint8Array?bytes:new Uint8Array(bytes);
  return TD.decode(u8);
}
function parseGpxBytes(bytes, fileName){
  var text=decode(bytes);
  var segs=scanSegments(text);
  if(!segs.length) return null;
  return buildPieces(segs,fileName);
}

/* ---------- FIT ---------- */
function readField(dv,p,f,le){
  var s=f.size;
  /* 不少设备（Zepp / Amazfit 等）会把 0x80 "endian ability" 标志并进 base type，
     例如 0x85=sint32、0x86=uint32、0x84=uint16，读之前先把高位剥掉。 */
  var t=f.type & 0x7F;
  try{
    if(t===0x01) return s>=1?dv.getInt8(p):0;
    if(t===0x02||t===0x0A||t===0x00||t===0x0D) return s>=1?dv.getUint8(p):0;
    if(t===0x03) return s>=2?dv.getInt16(p,le):0;
    if(t===0x04||t===0x0B) return s>=2?dv.getUint16(p,le):0;
    if(t===0x05) return s>=4?dv.getInt32(p,le):0;
    if(t===0x06||t===0x0C) return s>=4?dv.getUint32(p,le):0;
    if(t===0x08) return s>=4?dv.getFloat32(p,le):0;
    if(t===0x09) return s>=8?dv.getFloat64(p,le):0;
    if(t===0x07) return 0;                 // string，值用不到
  }catch(e){ return 0; }
  return 0;
}
function validLL(lat,lon){
  return lat!==INVALID_S32 && lon!==INVALID_S32 && (lat!==0||lon!==0);
}
function parseFit(bytes, fileName){
  var dv=viewOf(bytes), len=dv.byteLength;
  if(len<14) return null;
  var hs=dv.getUint8(0);
  if(hs!==12 && hs!==14) return null;
  var dataSize=dv.getUint32(4,true);
  var pos=hs, end=Math.min(len,hs+dataSize), defs=new Array(16), lastTs=-1;
  var all=[], i;
  var diag={msgs:0,records:0,withLL:0,defs:0};
  while(pos<end){
    var h=dv.getUint8(pos++);
    var local, def, p, f, v, ts, lat, lon;
    if(h & 0x80){
      /* 压缩时间戳头：数据里没有 timestamp 字段 */
      /* 压缩时间戳头：local message number 只有 2 位（bit5-6），低 5 位是秒偏移 */
      local=(h>>5)&3;
      var off=h&31;
      ts=(lastTs<0?0:(lastTs & ~31)) | off;
      if(lastTs>=0 && ts<=lastTs) ts+=32;
      lastTs=ts;
      def=defs[local];
      if(!def){ break; }
      diag.msgs++;
      if(def.global===20) diag.records++;
      p=pos; lat=null; lon=null;
      for(i=0;i<def.fields.length;i++){
        f=def.fields[i];
        if(f.num===253) continue;          // 压缩头省略
        v=readField(dv,p,f,def.le);
        if(f.num===0) lat=v; else if(f.num===1) lon=v;
        p+=f.size;
      }
      pos=p+def.devSize;
      if(def.global===20 && lat!==null && lon!==null && validLL(lat,lon)){
        diag.withLL++;
        all.push([lat*SEMI, lon*SEMI, ts?(FIT_EPOCH+ts)*1000:0]);
      }
    }else if(h & 0x40){
      /* 定义消息 */
      diag.defs++;
      local=h&15;
      pos++;                                // reserved
      var le=dv.getUint8(pos)===0; pos++;   // architecture: 0 = little endian
      var global=dv.getUint16(pos,le); pos+=2;
      var nf=dv.getUint8(pos++), fields=[], devSize=0, hasTs=false;
      for(i=0;i<nf;i++){
        var num=dv.getUint8(pos++), sz=dv.getUint8(pos++), type=dv.getUint8(pos++);
        fields.push({num:num,size:sz,type:type});
        if(num===253) hasTs=true;
      }
      if(h & 0x20){
        var nd=dv.getUint8(pos++);
        for(i=0;i<nd;i++){ devSize+=dv.getUint8(pos+1); pos+=3; }
      }
      defs[local]={global:global,le:le,fields:fields,devSize:devSize,hasTs:hasTs};
    }else{
      /* 普通数据消息 */
      local=h&15;
      def=defs[local];
      if(!def){ break; }
      diag.msgs++;
      if(def.global===20) diag.records++;
      p=pos; ts=null; lat=null; lon=null;
      for(i=0;i<def.fields.length;i++){
        f=def.fields[i];
        v=readField(dv,p,f,def.le);
        if(f.num===253) ts=v;
        else if(f.num===0) lat=v;
        else if(f.num===1) lon=v;
        p+=f.size;
      }
      pos=p+def.devSize;
      if(ts!==null && ts!==0xFFFFFFFF) lastTs=ts;
      else ts=lastTs;
      if(def.global===20 && lat!==null && lon!==null && validLL(lat,lon)){
        diag.withLL++;
        all.push([lat*SEMI, lon*SEMI, ts>=0?(FIT_EPOCH+ts)*1000:0]);
      }
    }
  }
  if(window.TRACK) window.TRACK.lastFit=diag;
  if(!all.length) return null;
  /* 长时间中断 / 大跳：切成新段，避免关机再开机连出一条横跨全城的直线 */
  var segs=[], cur=[all[0]];
  for(i=1;i<all.length;i++){
    var a=all[i-1], b=all[i];
    var gap=(a[2]&&b[2]&&b[2]-a[2]>30*60000) || haversine(a,b)>5000;
    if(gap){ segs.push({name:"",pts:cur}); cur=[]; }
    cur.push(b);
  }
  segs.push({name:"",pts:cur});
  return buildPieces(segs,fileName);
}

/* ---------- 对外接口 ---------- */
function isTrack(name){ return /\.(gpx|fit)$/i.test(name||""); }
function isZip(name,type){ return /\.zip$/i.test(name||"") || /zip/i.test(type||""); }
function parseAny(bytes, fileName){
  if(/\.fit$/i.test(fileName)) return parseFit(bytes,fileName);
  return parseGpxBytes(bytes,fileName);
}
function read(file){
  if(file.arrayBuffer) return file.arrayBuffer().then(function(b){ return new Uint8Array(b); });
  return new Promise(function(res,rej){
    var r=new FileReader();
    r.onload=function(){ res(new Uint8Array(r.result)); };
    r.onerror=function(){ rej(r.error); };
    r.readAsArrayBuffer(file);
  });
}
/* zip：手写 EOCD / 中央目录解析，只取 .gpx / .fit，支持 method 0 / 8 */
async function unzip(bytes){
  if(typeof DecompressionStream==="undefined") throw new Error("这个浏览器太旧，不支持解压，请改用 Chrome/Edge 新版");
  var u8=bytes instanceof Uint8Array?bytes:new Uint8Array(bytes);
  var dv=new DataView(u8.buffer,u8.byteOffset,u8.byteLength);
  var eocd=-1, stop=Math.max(0,u8.length-66000), i;
  for(i=u8.length-22;i>=stop;i--){ if(dv.getUint32(i,true)===0x06054b50){ eocd=i; break; } }
  if(eocd<0) throw new Error("不是有效的 zip 文件");
  var total=dv.getUint16(eocd+10,true), off=dv.getUint32(eocd+16,true), out=[];
  for(var k=0;k<total;k++){
    if(dv.getUint32(off,true)!==0x02014b50) break;
    var method=dv.getUint16(off+10,true), csize=dv.getUint32(off+20,true);
    var nlen=dv.getUint16(off+28,true), elen=dv.getUint16(off+30,true), clen=dv.getUint16(off+32,true);
    var lho=dv.getUint32(off+42,true);
    var raw=u8.subarray(off+46,off+46+nlen);
    off+=46+nlen+elen+clen;
    var path=TD.decode(raw);
    if(path.endsWith("/") || !isTrack(path)) continue;
    var lnlen=dv.getUint16(lho+26,true), lelen=dv.getUint16(lho+28,true);
    var start=lho+30+lnlen+lelen;
    var data=u8.subarray(start,start+csize), payload;
    if(method===0) payload=data.slice();
    else if(method===8){
      var st=new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
      payload=new Uint8Array(await new Response(st).arrayBuffer());
    }else continue;
    out.push({name:path.split("/").pop(), bytes:payload});
  }
  return out;
}
/* 让出主线程：比 setTimeout(0) 少一档 4ms 钳制，导入时进度能刷新 */
function yieldUI(){
  return new Promise(function(res){
    if(typeof MessageChannel!=="undefined"){
      try{ var ch=new MessageChannel(); ch.port1.onmessage=function(){res();}; ch.port2.postMessage(0); return; }catch(e){}
    }
    setTimeout(res,0);
  });
}

/* 解析失败时给用户看得懂的诊断，别只丢一句「没有轨迹点」 */
function noDataMessage(zips, tracks){
  var msg="没有解析出轨迹点。\nGPX 需要含 <trkpt> / <rtept>；FIT 需要 record 里的经纬度。";
  var onlyFit = !zips.length && tracks.length &&
                tracks.every(function(f){ return /\.fit$/i.test(f.name||""); });
  var d=window.TRACK && window.TRACK.lastFit;
  if(onlyFit && d){
    msg += "\n\n诊断：读到 "+d.msgs+" 条消息 / "+d.defs+" 个定义，record "
         + d.records + " 条，其中带经纬度 " + d.withLL + " 条。";
    if(!d.records) msg += "\n这个文件里没有 record(20) 轨迹消息，可能是纯心率 / 睡眠数据。";
    else if(!d.withLL) msg += "\nrecord 里没读到经纬度，可能把 GPS 放在自定义开发者字段里了。";
  }
  return msg;
}

/* ---------- 坐标打包：把 [[lat,lon],...] 变成 ×1e5 的整数差值 ----------
   轨迹相邻点通常只差几十米，差值只有一两位数字；扁平整数数组比
   [[30.274030,120.161860],...] 小一半以上，JSON.parse 也更快。
   第一点存的是相对 0 的差值（即绝对值），解码时统一累加即可。
   精度 1e-5 度约 1 米，对地图显示足够；旧格式由 decodeSeg 兼容。 */
function packSeg(pts){
  var out=[], plat=0, plon=0;
  for(var i=0;i<pts.length;i++){
    var lat=Math.round(pts[i][0]*1e5), lon=Math.round(pts[i][1]*1e5);
    out.push(lat-plat, lon-plon);
    plat=lat; plon=lon;
  }
  return out;
}
function unpackSeg(flat){
  var pts=[], lat=0, lon=0;
  for(var i=0;i<flat.length;i+=2){
    lat+=flat[i]; lon+=flat[i+1];
    pts.push([lat/1e5, lon/1e5]);
  }
  return pts;
}
function decodeSeg(seg){
  if(!seg || !seg.length) return [];
  return Array.isArray(seg[0]) ? seg : unpackSeg(seg);
}

/* ---------- 快照导出：把当前轨迹固化进一份自包含 HTML ----------
   浏览器本地缓存有容量上限，网盘 / 临时目录打开的页面还经常被禁用，
   刷新后就丢了。导出时克隆当前文档、清掉运行时生成的 DOM，再把数据写回
   #preset。存成文件后，打开就是完整数据，不依赖任何浏览器存储。 */
function snapshotPayload(days){
  return {
    generated:new Date().toLocaleString("zh-CN"),
    root:"浏览器导入",
    days:days.map(function(d){
      return {key:d.key, items:d.items.map(function(it){
        return {
          file:it.file, name:it.name||"",
          keyTime:it.keyTime||null, keyFile:it.keyFile||null, keyStart:it.keyStart||null,
          dist:Math.round((it.dist||0)*10)/10, pts:it.pts||0,
          start:it.start||"", end:it.end||"", t0:it.t0||"",
          part:it.part||1, parts:it.parts||1,
          segs:(it.segs||[]).map(function(s){ return packSeg(s); })
        };
      })};
    })
  };
}
function buildSnapshotHTML(days, cleanSelectors){
  var root=document.documentElement.cloneNode(true);
  (cleanSelectors||[]).forEach(function(sel){
    var el=root.querySelector(sel);
    if(el) el.innerHTML="";
  });
  ["#toast","#drop","#side","#batchbar"].forEach(function(sel){
    var el=root.querySelector(sel); if(el) el.classList.remove("on");
  });
  var panel=root.querySelector("#panel"); if(panel) panel.classList.remove("hide");
  var ts=root.querySelector("#toast"); if(ts) ts.textContent="";
  var w=root.querySelector("#welcome"); if(w) w.removeAttribute("style");
  root.querySelectorAll("canvas.heatcanvas").forEach(function(c){ c.remove(); });
  var ps=root.querySelector("#preset");
  if(!ps) return null;
  var data=JSON.stringify(snapshotPayload(days));
  ps.textContent=data.replace(/<\//g,"<\\/");
  return "<!doctype html>\n"+root.outerHTML;
}
function exportSnapshot(days, cleanSelectors){
  var html=buildSnapshotHTML(days,cleanSelectors);
  if(!html) return false;
  try{
    var blob=new Blob([html],{type:"text/html;charset=utf-8"});
    var url=URL.createObjectURL(blob);
    var a=document.createElement("a");
    a.href=url;
    a.download="轨迹快照_"+new Date().toISOString().slice(0,10)+".html";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function(){ URL.revokeObjectURL(url); },30000);
    return true;
  }catch(e){ return false; }
}

window.TRACK={
  isTrack:isTrack, isZip:isZip, read:read, decode:decode, unzip:unzip,
  parseAny:parseAny, parseGpxBytes:parseGpxBytes, parseFit:parseFit,
  yieldUI:yieldUI, isoMs:isoMs, haversine:haversine,
  noDataMessage:noDataMessage, lastFit:null,
  packSeg:packSeg, unpackSeg:unpackSeg, decodeSeg:decodeSeg,
  buildSnapshotHTML:buildSnapshotHTML, exportSnapshot:exportSnapshot
};
})();
