document.addEventListener('click', function (event) {
    if (event.target.closest('[data-print]')) window.print();
});

document.querySelectorAll('input[type="password"]').forEach(function (field, index) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'senha-controle secondary';
    toggle.textContent = 'Mostrar senha';
    toggle.setAttribute('aria-pressed', 'false');
    toggle.addEventListener('click', function () {
        const show = field.type === 'password';
        field.type = show ? 'text' : 'password';
        toggle.textContent = show ? 'Ocultar senha' : 'Mostrar senha';
        toggle.setAttribute('aria-pressed', String(show));
    });
    field.insertAdjacentElement('afterend', toggle);
    if (field.autocomplete !== 'new-password') return;

    // Count complete Unicode characters just like the server; do not truncate input.
    field.removeAttribute('maxlength');
    field.removeAttribute('minlength');
    const list = document.createElement('ul');
    list.className = 'senha-regras';
    list.id = 'senha-regras-' + index;
    const labels = ['8 caracteres', 'Pelo menos um número', 'Pelo menos um caractere especial'];
    const items = labels.map(function (label) {
        const item = document.createElement('li');
        item.textContent = label;
        list.appendChild(item);
        return item;
    });
    const result = document.createElement('p');
    result.className = 'senha-resultado';
    result.setAttribute('aria-live', 'polite');
    toggle.insertAdjacentElement('afterend', list);
    list.insertAdjacentElement('afterend', result);
    field.setAttribute('aria-describedby', [field.getAttribute('aria-describedby') || '', list.id].join(' ').trim());
    function validate() {
        const value = field.value;
        const count = Array.from(value).length;
        const rules = [count === 8, /[0-9]/.test(value), /[^\p{L}\p{N}\s]/u.test(value)];
        items.forEach(function (item, i) {
            item.dataset.ok = String(rules[i]);
            item.textContent = labels[i] + (i === 0 ? ' (' + count + '/8)' : '') + (rules[i] ? ' — atendido' : '');
        });
        const valid = rules.every(Boolean);
        field.setCustomValidity(value && !valid ? 'Use exatamente 8 caracteres, com pelo menos um número e um caractere especial.' : '');
        result.textContent = value ? (valid ? 'Senha válida para o cadastro.' : 'Confira os requisitos acima.') : '';
        result.dataset.ok = String(valid);
    }
    field.addEventListener('input', validate);
    field.addEventListener('change', validate);
    if (field.form) field.form.addEventListener('submit', function (event) {
        validate();
        if (!field.checkValidity()) { event.preventDefault(); field.reportValidity(); }
    });
    validate();
});

// FORMULÁRIOS — validação de anexos e proteção contra cliques repetidos.
// Não altera permissões, valores enviados ou regras de aprovação do servidor.
document.querySelectorAll('form').forEach(function (form) {
    if (form.method.toLowerCase() !== 'post') return;
    let sending = false;
    let timer;
    const files = Array.from(form.querySelectorAll('input[type="file"]'));
    function validateFiles() {
        let total = 0;
        files.forEach(function (field) {
            field.setCustomValidity('');
            Array.from(field.files || []).forEach(function (file) { total += file.size; });
        });
        if (total > 16 * 1024 * 1024 && files.length) {
            files[0].setCustomValidity('Os anexos deste envio devem somar no máximo 16 MB. Selecione arquivos menores.');
        }
    }
    files.forEach(function (field) { field.addEventListener('change', validateFiles); });
    const status = document.createElement('p');
    status.className = 'formulario-status';
    status.setAttribute('role', 'status');
    status.hidden = true;
    form.appendChild(status);
    function reset() {
        sending = false;
        clearTimeout(timer);
        form.removeAttribute('aria-busy');
        status.hidden = true;
        status.textContent = '';
    }
    form.addEventListener('submit', function (event) {
        if (event.defaultPrevented) return;
        validateFiles();
        if (!form.checkValidity()) { event.preventDefault(); form.reportValidity(); return; }
        if (sending) { event.preventDefault(); return; }
        sending = true;
        form.setAttribute('aria-busy', 'true');
        status.textContent = 'Enviando. Aguarde…';
        status.hidden = false;
        // Os botões não são desabilitados: seus nomes/valores podem definir a ação.
        // Libera novas tentativas se a página permanecer aberta por falha de rede.
        timer = setTimeout(reset, 15000);
    });
    window.addEventListener('pageshow', reset);
});
