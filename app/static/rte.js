// 서술 항목(주요 업무 진행 현황 등) 리치 에디터 — 서식(굵게/기울임/밑줄/목록)·표 삽입·이미지
// 붙여넣기. 외부 라이브러리 없이 execCommand + contenteditable로 구현(2026-08-22 신설,
// 인터넷이 안 되는 사내망 서버라 CDN 라이브러리를 못 쓴다 — 이 프로젝트의 기존 원칙과 동일).
(function () {
  function insertTable() {
    var html = '<table><tbody>';
    for (var r = 0; r < 3; r++) {
      html += '<tr>';
      for (var c = 0; c < 3; c++) html += '<td>&nbsp;</td>';
      html += '</tr>';
    }
    html += '</tbody></table><p><br></p>';
    document.execCommand('insertHTML', false, html);
  }

  function uploadImage(root, file, onDone) {
    var fd = new FormData();
    fd.append('ym', root.dataset.ym);
    fd.append('section', root.dataset.section);
    fd.append('file', file);
    fetch('/report-text/image', { method: 'POST', body: fd })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.url) onDone(data.url);
        else alert(data.error || '이미지 업로드에 실패했습니다');
      })
      .catch(function () { alert('이미지 업로드에 실패했습니다(네트워크 오류)'); });
  }

  function initOne(root) {
    var editor = root.querySelector('.rte-editor');
    var hidden = root.querySelector('textarea');
    if (!editor || !hidden) return;
    editor.innerHTML = hidden.value;      // 서버가 이미 HTML로(레거시 텍스트도 변환해) 내려준 값

    function sync() { hidden.value = editor.innerHTML; }
    editor.addEventListener('input', sync);
    // 폼 전송 직전 한 번 더 동기화 — 버튼 클릭 직후 등 input 이벤트가 안 뜨는 경우의 안전망.
    var form = root.closest('form');
    if (form) form.addEventListener('submit', sync);

    root.querySelectorAll('.rte-btn').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        editor.focus();
        var cmd = btn.dataset.cmd;
        if (cmd === 'table') insertTable();
        else document.execCommand(cmd, false, null);
        sync();
      });
    });

    editor.addEventListener('paste', function (e) {
      var cd = e.clipboardData || window.clipboardData;
      var items = cd && cd.items;
      if (!items) return;
      for (var i = 0; i < items.length; i++) {
        if (items[i].type.indexOf('image') === 0) {
          e.preventDefault();
          var file = items[i].getAsFile();
          uploadImage(root, file, function (url) {
            editor.focus();
            document.execCommand('insertImage', false, url);
            sync();
          });
          return;
        }
      }
      // 이미지가 아니면(글자 붙여넣기 등) 기본 동작을 그대로 둔다.
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.rte').forEach(initOne);
  });
})();
