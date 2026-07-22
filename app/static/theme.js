// 라이트/다크 전환 (모든 화면 공통)
(function(){
  var btn = document.getElementById("themeBtn");
  var root = document.documentElement;
  function set(t){
    if(t==="dark"){ root.setAttribute("data-theme","dark"); if(btn) btn.textContent="☀ 라이트"; }
    else { root.removeAttribute("data-theme"); if(btn) btn.textContent="🌙 다크"; }
    try{ localStorage.setItem("qms-theme", t); }catch(e){}
  }
  try{ set(localStorage.getItem("qms-theme")||"light"); }catch(e){ set("light"); }
  if(btn) btn.addEventListener("click", function(){
    set(root.getAttribute("data-theme")==="dark"?"light":"dark");
  });
})();
