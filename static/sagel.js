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
