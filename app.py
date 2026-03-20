# ============================================
# SISTEMA DE CONTROLE DE RIFA
# Suporte a múltiplas rifas + Autenticação
# ============================================

import os
import csv
import io
import json
import random
import sqlite3
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = 'rifa-facil-2026-seguro'

# Prefixo para rodar em schumaker.com.br/rifafacil
APPLICATION_PREFIX = os.environ.get('APP_PREFIX', '/rifafacil')
app.config['APPLICATION_ROOT'] = APPLICATION_PREFIX

# Corrige headers quando atrás de proxy reverso (Nginx)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# --- Configuração do Banco de Dados ---
DATABASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'database')
DATABASE_PATH = os.path.join(DATABASE_DIR, 'rifa.db')


def get_db():
    """Conecta ao banco de dados SQLite."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    """Cria o banco de dados e as tabelas se não existirem."""
    os.makedirs(DATABASE_DIR, exist_ok=True)
    conn = get_db()

    # Tabela de rifas
    conn.execute('''
        CREATE TABLE IF NOT EXISTS rifas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            descricao TEXT,
            quantidade_numeros INTEGER NOT NULL,
            valor_numero REAL NOT NULL DEFAULT 0,
            chave_pix TEXT,
            whatsapp_gerente TEXT,
            data_criacao TEXT NOT NULL
        )
    ''')

    # Tabela de compras
    conn.execute('''
        CREATE TABLE IF NOT EXISTS compras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rifa_id INTEGER NOT NULL,
            numero INTEGER NOT NULL,
            nome TEXT NOT NULL,
            telefone TEXT,
            data_compra TEXT NOT NULL,
            FOREIGN KEY (rifa_id) REFERENCES rifas(id) ON DELETE CASCADE,
            UNIQUE(rifa_id, numero)
        )
    ''')

    # Tabela de usuários
    conn.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            nome TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            ativo INTEGER NOT NULL DEFAULT 1,
            data_criacao TEXT NOT NULL
        )
    ''')

    # Tabela de sorteios
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sorteios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rifa_id INTEGER NOT NULL,
            numeros_sorteados TEXT NOT NULL,
            fonte TEXT NOT NULL,
            num_min INTEGER,
            num_max INTEGER,
            data_sorteio TEXT NOT NULL,
            realizado_por TEXT,
            FOREIGN KEY (rifa_id) REFERENCES rifas(id) ON DELETE CASCADE
        )
    ''')

    # Migração: adiciona colunas novas se não existirem
    try:
        conn.execute('ALTER TABLE rifas ADD COLUMN chave_pix TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute('ALTER TABLE rifas ADD COLUMN whatsapp_gerente TEXT')
    except sqlite3.OperationalError:
        pass

    # Seed do superadmin (se não existir)
    existing = conn.execute('SELECT id FROM usuarios WHERE role = ?', ('superadmin',)).fetchone()
    if not existing:
        conn.execute(
            'INSERT INTO usuarios (username, senha_hash, nome, role, ativo, data_criacao) VALUES (?, ?, ?, ?, ?, ?)',
            ('admin', generate_password_hash('admin123'), 'Administrador', 'superadmin', 1,
             datetime.now().strftime('%d/%m/%Y %H:%M'))
        )

    conn.commit()
    conn.close()


# =============================
#  AUTENTICAÇÃO
# =============================

def get_current_user():
    """Retorna o usuário logado ou None."""
    user_id = session.get('user_id')
    if not user_id:
        return None
    conn = get_db()
    user = conn.execute('SELECT * FROM usuarios WHERE id = ? AND ativo = 1', (user_id,)).fetchone()
    conn.close()
    return user


def login_required(f):
    """Decorator: requer login."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            flash('Faça login para acessar.', 'erro')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def superadmin_required(f):
    """Decorator: requer superadmin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or user['role'] != 'superadmin':
            flash('Acesso restrito ao super administrador.', 'erro')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


@app.context_processor
def inject_user():
    """Injeta o usuário atual em todos os templates."""
    return dict(current_user=get_current_user())


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Página de login."""
    if session.get('user_id'):
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        senha = request.form.get('senha', '')

        conn = get_db()
        user = conn.execute(
            'SELECT * FROM usuarios WHERE username = ? AND ativo = 1', (username,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user['senha_hash'], senha):
            session['user_id'] = user['id']
            session['user_role'] = user['role']
            session['user_nome'] = user['nome']
            flash(f'Bem-vindo, {user["nome"]}!', 'sucesso')
            return redirect(url_for('index'))
        else:
            flash('Usuário ou senha incorretos.', 'erro')

    return render_template('login.html')


@app.route('/logout')
def logout():
    """Encerra a sessão."""
    session.clear()
    flash('Sessão encerrada.', 'sucesso')
    return redirect(url_for('index'))


# =============================
#  GESTÃO DE ADMINS
# =============================

@app.route('/admin/usuarios')
@login_required
@superadmin_required
def admin_usuarios():
    """Lista de administradores."""
    conn = get_db()
    usuarios = conn.execute('SELECT * FROM usuarios ORDER BY role DESC, nome').fetchall()
    conn.close()
    return render_template('admin_usuarios.html', usuarios=usuarios)


@app.route('/admin/usuarios/novo', methods=['POST'])
@login_required
@superadmin_required
def novo_usuario():
    """Cria um novo admin."""
    username = request.form.get('username', '').strip().lower()
    nome = request.form.get('nome', '').strip()
    senha = request.form.get('senha', '')

    if not username or not nome or not senha:
        flash('Todos os campos são obrigatórios!', 'erro')
        return redirect(url_for('admin_usuarios'))

    if len(senha) < 4:
        flash('Senha deve ter pelo menos 4 caracteres.', 'erro')
        return redirect(url_for('admin_usuarios'))

    conn = get_db()
    existing = conn.execute('SELECT id FROM usuarios WHERE username = ?', (username,)).fetchone()
    if existing:
        flash(f'Usuário "{username}" já existe!', 'erro')
        conn.close()
        return redirect(url_for('admin_usuarios'))

    conn.execute(
        'INSERT INTO usuarios (username, senha_hash, nome, role, ativo, data_criacao) VALUES (?, ?, ?, ?, ?, ?)',
        (username, generate_password_hash(senha), nome, 'admin', 1,
         datetime.now().strftime('%d/%m/%Y %H:%M'))
    )
    conn.commit()
    conn.close()

    flash(f'Admin "{nome}" criado com sucesso!', 'sucesso')
    return redirect(url_for('admin_usuarios'))


@app.route('/admin/usuarios/excluir/<int:user_id>', methods=['POST'])
@login_required
@superadmin_required
def excluir_usuario(user_id):
    """Exclui um admin."""
    conn = get_db()
    user = conn.execute('SELECT * FROM usuarios WHERE id = ?', (user_id,)).fetchone()
    if not user:
        flash('Usuário não encontrado.', 'erro')
    elif user['role'] == 'superadmin':
        flash('Não é possível excluir o super administrador.', 'erro')
    else:
        conn.execute('DELETE FROM usuarios WHERE id = ?', (user_id,))
        conn.commit()
        flash(f'Admin "{user["nome"]}" removido.', 'sucesso')
    conn.close()
    return redirect(url_for('admin_usuarios'))


@app.route('/admin/usuarios/alterar-senha', methods=['POST'])
@login_required
def alterar_senha():
    """Altera a senha do usuário logado."""
    senha_atual = request.form.get('senha_atual', '')
    nova_senha = request.form.get('nova_senha', '')

    if len(nova_senha) < 4:
        flash('Nova senha deve ter pelo menos 4 caracteres.', 'erro')
        return redirect(url_for('admin_usuarios'))

    conn = get_db()
    user = conn.execute('SELECT * FROM usuarios WHERE id = ?', (session['user_id'],)).fetchone()

    if not check_password_hash(user['senha_hash'], senha_atual):
        flash('Senha atual incorreta.', 'erro')
        conn.close()
        return redirect(url_for('admin_usuarios'))

    conn.execute('UPDATE usuarios SET senha_hash = ? WHERE id = ?',
                 (generate_password_hash(nova_senha), session['user_id']))
    conn.commit()
    conn.close()
    flash('Senha alterada com sucesso!', 'sucesso')
    return redirect(url_for('admin_usuarios'))


# =============================
#  ROTAS - RIFAS
# =============================

@app.route('/')
def index():
    """Página inicial - lista todas as rifas cadastradas."""
    conn = get_db()
    rifas = conn.execute('SELECT * FROM rifas ORDER BY data_criacao DESC').fetchall()

    stats = {}
    for rifa in rifas:
        vendidos = conn.execute(
            'SELECT COUNT(*) as total FROM compras WHERE rifa_id = ?', (rifa['id'],)
        ).fetchone()['total']
        total_arrecadado = vendidos * rifa['valor_numero'] if rifa['valor_numero'] > 0 else 0
        total_possivel = rifa['quantidade_numeros'] * rifa['valor_numero'] if rifa['valor_numero'] > 0 else 0
        stats[rifa['id']] = {
            'vendidos': vendidos,
            'disponiveis': rifa['quantidade_numeros'] - vendidos,
            'percentual': round((vendidos / rifa['quantidade_numeros']) * 100, 1) if rifa['quantidade_numeros'] > 0 else 0,
            'arrecadado': total_arrecadado,
            'faltando': total_possivel - total_arrecadado,
        }

    conn.close()
    return render_template('index.html', rifas=rifas, stats=stats)


@app.route('/nova-rifa', methods=['POST'])
@login_required
def nova_rifa():
    """Cria uma nova rifa."""
    nome = request.form.get('nome', '').strip()
    descricao = request.form.get('descricao', '').strip()
    quantidade = request.form.get('quantidade_numeros', type=int)
    valor = request.form.get('valor_numero', type=float) or 0
    chave_pix = request.form.get('chave_pix', '').strip()
    whatsapp_gerente = request.form.get('whatsapp_gerente', '').strip()

    if not nome:
        flash('O nome da rifa é obrigatório!', 'erro')
        return redirect(url_for('index'))

    if not quantidade or quantidade < 1 or quantidade > 1000:
        flash('A quantidade deve ser entre 1 e 1000!', 'erro')
        return redirect(url_for('index'))

    conn = get_db()
    data_criacao = datetime.now().strftime('%d/%m/%Y %H:%M')
    conn.execute(
        'INSERT INTO rifas (nome, descricao, quantidade_numeros, valor_numero, chave_pix, whatsapp_gerente, data_criacao) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (nome, descricao, quantidade, valor, chave_pix, whatsapp_gerente, data_criacao)
    )
    conn.commit()
    conn.close()

    flash(f'Rifa "{nome}" criada com sucesso!', 'sucesso')
    return redirect(url_for('index'))


@app.route('/excluir-rifa/<int:rifa_id>', methods=['POST'])
@login_required
def excluir_rifa(rifa_id):
    """Exclui uma rifa e todas as suas compras."""
    conn = get_db()
    rifa = conn.execute('SELECT nome FROM rifas WHERE id = ?', (rifa_id,)).fetchone()
    if rifa:
        conn.execute('DELETE FROM compras WHERE rifa_id = ?', (rifa_id,))
        conn.execute('DELETE FROM sorteios WHERE rifa_id = ?', (rifa_id,))
        conn.execute('DELETE FROM rifas WHERE id = ?', (rifa_id,))
        conn.commit()
        flash(f'Rifa "{rifa["nome"]}" excluída.', 'sucesso')
    else:
        flash('Rifa não encontrada.', 'erro')
    conn.close()
    return redirect(url_for('index'))


@app.route('/editar-rifa/<int:rifa_id>', methods=['GET'])
@login_required
def editar_rifa_page(rifa_id):
    """Página de edição de rifa."""
    conn = get_db()
    rifa = conn.execute('SELECT * FROM rifas WHERE id = ?', (rifa_id,)).fetchone()
    if not rifa:
        flash('Rifa não encontrada.', 'erro')
        conn.close()
        return redirect(url_for('index'))
    conn.close()
    return render_template('editar_rifa.html', rifa=rifa)


@app.route('/editar-rifa/<int:rifa_id>', methods=['POST'])
@login_required
def editar_rifa(rifa_id):
    """Salva as alterações de uma rifa."""
    nome = request.form.get('nome', '').strip()
    descricao = request.form.get('descricao', '').strip()
    valor = request.form.get('valor_numero', type=float) or 0
    chave_pix = request.form.get('chave_pix', '').strip()
    whatsapp_gerente = request.form.get('whatsapp_gerente', '').strip()

    if not nome:
        flash('O nome da rifa é obrigatório!', 'erro')
        return redirect(url_for('editar_rifa_page', rifa_id=rifa_id))

    conn = get_db()
    conn.execute(
        'UPDATE rifas SET nome = ?, descricao = ?, valor_numero = ?, chave_pix = ?, whatsapp_gerente = ? WHERE id = ?',
        (nome, descricao, valor, chave_pix, whatsapp_gerente, rifa_id)
    )
    conn.commit()
    conn.close()

    flash(f'Rifa "{nome}" atualizada com sucesso!', 'sucesso')
    return redirect(url_for('index'))


# =============================
#  ROTAS - CARTELA DA RIFA
# =============================

@app.route('/rifa/<int:rifa_id>')
def cartela(rifa_id):
    """Página da cartela individual de uma rifa."""
    conn = get_db()
    rifa = conn.execute('SELECT * FROM rifas WHERE id = ?', (rifa_id,)).fetchone()
    if not rifa:
        flash('Rifa não encontrada.', 'erro')
        conn.close()
        return redirect(url_for('index'))

    vendidos = conn.execute(
        'SELECT numero, nome, telefone FROM compras WHERE rifa_id = ? ORDER BY numero',
        (rifa_id,)
    ).fetchall()
    conn.close()

    mapa_vendidos = {row['numero']: {'nome': row['nome'], 'telefone': row['telefone']} for row in vendidos}

    return render_template('cartela.html', rifa=rifa, mapa_vendidos=mapa_vendidos)


@app.route('/rifa/<int:rifa_id>/comprar', methods=['POST'])
@login_required
def comprar(rifa_id):
    """Registra a compra de um ou mais números dentro de uma rifa."""
    numeros_raw = request.form.get('numeros', '').strip()
    nome = request.form.get('nome', '').strip()
    telefone = request.form.get('telefone', '').strip()

    if not numeros_raw or not nome:
        flash('Nome e números são obrigatórios!', 'erro')
        return redirect(url_for('cartela', rifa_id=rifa_id))

    try:
        numeros = [int(n.strip()) for n in numeros_raw.split(',') if n.strip()]
    except ValueError:
        flash('Números inválidos! Use apenas números separados por vírgula.', 'erro')
        return redirect(url_for('cartela', rifa_id=rifa_id))

    if not numeros:
        flash('Selecione ao menos um número.', 'erro')
        return redirect(url_for('cartela', rifa_id=rifa_id))

    conn = get_db()
    rifa = conn.execute('SELECT quantidade_numeros FROM rifas WHERE id = ?', (rifa_id,)).fetchone()
    if not rifa:
        flash('Rifa não encontrada.', 'erro')
        conn.close()
        return redirect(url_for('index'))

    invalidos = [n for n in numeros if n < 1 or n > rifa['quantidade_numeros']]
    if invalidos:
        flash(f'Números fora do intervalo: {", ".join(str(n) for n in invalidos)}', 'erro')
        conn.close()
        return redirect(url_for('cartela', rifa_id=rifa_id))

    placeholders = ','.join('?' * len(numeros))
    ja_vendidos = conn.execute(
        f'SELECT numero FROM compras WHERE rifa_id = ? AND numero IN ({placeholders})',
        [rifa_id] + numeros
    ).fetchall()

    if ja_vendidos:
        nomes = ', '.join(str(r['numero']) for r in ja_vendidos)
        flash(f'Os números {nomes} já foram vendidos!', 'erro')
        conn.close()
        return redirect(url_for('cartela', rifa_id=rifa_id))

    data_compra = datetime.now().strftime('%d/%m/%Y %H:%M')
    try:
        for numero in numeros:
            conn.execute(
                'INSERT INTO compras (rifa_id, numero, nome, telefone, data_compra) VALUES (?, ?, ?, ?, ?)',
                (rifa_id, numero, nome, telefone, data_compra)
            )
        conn.commit()
        if len(numeros) == 1:
            flash(f'Número {numeros[0]} registrado para {nome}!', 'sucesso')
        else:
            flash(f'{len(numeros)} números registrados para {nome}!', 'sucesso')
    except sqlite3.IntegrityError:
        conn.rollback()
        flash('Erro ao registrar: algum número já foi vendido.', 'erro')
    finally:
        conn.close()

    return redirect(url_for('cartela', rifa_id=rifa_id))


# =============================
#  ROTAS - COMPRADORES
# =============================

@app.route('/rifa/<int:rifa_id>/compradores')
@login_required
def compradores(rifa_id):
    """Lista de compradores de uma rifa específica."""
    conn = get_db()
    rifa = conn.execute('SELECT * FROM rifas WHERE id = ?', (rifa_id,)).fetchone()
    if not rifa:
        flash('Rifa não encontrada.', 'erro')
        conn.close()
        return redirect(url_for('index'))

    lista = conn.execute(
        'SELECT id, numero, nome, telefone, data_compra FROM compras WHERE rifa_id = ? ORDER BY numero',
        (rifa_id,)
    ).fetchall()
    conn.close()

    return render_template('compradores.html', rifa=rifa, compradores=lista)


@app.route('/rifa/<int:rifa_id>/excluir-compra/<int:compra_id>', methods=['POST'])
@login_required
def excluir_compra(rifa_id, compra_id):
    """Exclui um registro de compra, liberando o número."""
    conn = get_db()
    registro = conn.execute(
        'SELECT numero, nome FROM compras WHERE id = ? AND rifa_id = ?',
        (compra_id, rifa_id)
    ).fetchone()

    if registro:
        conn.execute('DELETE FROM compras WHERE id = ?', (compra_id,))
        conn.commit()
        flash(f'Número {registro["numero"]} ({registro["nome"]}) foi removido.', 'sucesso')
    else:
        flash('Registro não encontrado.', 'erro')

    conn.close()
    return redirect(url_for('compradores', rifa_id=rifa_id))


@app.route('/rifa/<int:rifa_id>/editar-compra/<int:compra_id>', methods=['POST'])
@login_required
def editar_compra(rifa_id, compra_id):
    """Edita os dados de uma compra (nome e telefone)."""
    nome = request.form.get('nome', '').strip()
    telefone = request.form.get('telefone', '').strip()

    if not nome:
        flash('O nome é obrigatório!', 'erro')
        return redirect(url_for('compradores', rifa_id=rifa_id))

    conn = get_db()
    conn.execute(
        'UPDATE compras SET nome = ?, telefone = ? WHERE id = ? AND rifa_id = ?',
        (nome, telefone, compra_id, rifa_id)
    )
    conn.commit()
    conn.close()

    flash(f'Dados do comprador atualizados!', 'sucesso')
    return redirect(url_for('compradores', rifa_id=rifa_id))


@app.route('/rifa/<int:rifa_id>/exportar')
@login_required
def exportar_csv(rifa_id):
    """Exporta a lista de compradores como CSV."""
    conn = get_db()
    rifa = conn.execute('SELECT nome FROM rifas WHERE id = ?', (rifa_id,)).fetchone()
    if not rifa:
        conn.close()
        flash('Rifa não encontrada.', 'erro')
        return redirect(url_for('index'))

    compras = conn.execute(
        'SELECT numero, nome, telefone, data_compra FROM compras WHERE rifa_id = ? ORDER BY numero',
        (rifa_id,)
    ).fetchall()
    conn.close()

    output = io.StringIO()
    output.write('\ufeff')  # BOM for Excel
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['Número', 'Nome', 'Telefone', 'Data da Compra'])
    for c in compras:
        writer.writerow([c['numero'], c['nome'], c['telefone'] or '', c['data_compra']])

    filename = f'compradores_{rifa["nome"].replace(" ", "_").lower()}.csv'
    return Response(
        output.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


# =============================
#  ROTAS - SORTEIO
# =============================

@app.route('/rifa/<int:rifa_id>/sorteio')
@login_required
def sorteio(rifa_id):
    """Página do sorteador de números."""
    conn = get_db()
    rifa = conn.execute('SELECT * FROM rifas WHERE id = ?', (rifa_id,)).fetchone()
    if not rifa:
        flash('Rifa não encontrada.', 'erro')
        conn.close()
        return redirect(url_for('index'))

    vendidos_count = conn.execute(
        'SELECT COUNT(*) as total FROM compras WHERE rifa_id = ?', (rifa_id,)
    ).fetchone()['total']

    # Histórico de sorteios
    historico = conn.execute(
        'SELECT * FROM sorteios WHERE rifa_id = ? ORDER BY data_sorteio DESC LIMIT 20',
        (rifa_id,)
    ).fetchall()

    conn.close()

    return render_template('sorteio.html', rifa=rifa, vendidos_count=vendidos_count, historico=historico)


@app.route('/rifa/<int:rifa_id>/sortear', methods=['POST'])
@login_required
def sortear(rifa_id):
    """API para sortear números. Retorna JSON."""
    conn = get_db()
    rifa = conn.execute('SELECT * FROM rifas WHERE id = ?', (rifa_id,)).fetchone()
    if not rifa:
        conn.close()
        return jsonify({'erro': 'Rifa não encontrada.'}), 404

    data = request.get_json()
    quantidade = data.get('quantidade', 1)
    fonte = data.get('fonte', 'todos')
    num_min = data.get('num_min', 1)
    num_max = data.get('num_max', rifa['quantidade_numeros'])

    if quantidade < 1 or quantidade > 100:
        conn.close()
        return jsonify({'erro': 'Quantidade deve ser entre 1 e 100.'}), 400

    if num_min < 1:
        num_min = 1
    if num_max > rifa['quantidade_numeros']:
        num_max = rifa['quantidade_numeros']
    if num_min > num_max:
        conn.close()
        return jsonify({'erro': 'Intervalo inválido.'}), 400

    if fonte == 'vendidos':
        rows = conn.execute(
            'SELECT numero, nome, telefone FROM compras WHERE rifa_id = ? AND numero >= ? AND numero <= ? ORDER BY numero',
            (rifa_id, num_min, num_max)
        ).fetchall()
        pool = [{'numero': r['numero'], 'nome': r['nome'], 'telefone': r['telefone'] or ''} for r in rows]
    else:
        vendidos = conn.execute(
            'SELECT numero, nome, telefone FROM compras WHERE rifa_id = ? AND numero >= ? AND numero <= ?',
            (rifa_id, num_min, num_max)
        ).fetchall()
        mapa = {r['numero']: {'nome': r['nome'], 'telefone': r['telefone'] or ''} for r in vendidos}
        pool = []
        for n in range(num_min, num_max + 1):
            if n in mapa:
                pool.append({'numero': n, 'nome': mapa[n]['nome'], 'telefone': mapa[n]['telefone']})
            else:
                pool.append({'numero': n, 'nome': '', 'telefone': ''})

    if len(pool) == 0:
        conn.close()
        return jsonify({'erro': 'Nenhum número disponível para sorteio neste intervalo.'}), 400

    if quantidade > len(pool):
        quantidade = len(pool)

    sorteados = random.sample(pool, quantidade)

    # Salva no histórico
    user = get_current_user()
    conn.execute(
        'INSERT INTO sorteios (rifa_id, numeros_sorteados, fonte, num_min, num_max, data_sorteio, realizado_por) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (rifa_id, json.dumps(sorteados, ensure_ascii=False), fonte, num_min, num_max,
         datetime.now().strftime('%d/%m/%Y %H:%M'), user['nome'] if user else 'Sistema')
    )
    conn.commit()
    conn.close()

    return jsonify({'sorteados': sorteados, 'total_pool': len(pool)})


# --- Inicialização ---
if __name__ == '__main__':
    init_db()
    print('=' * 50)
    print('  SISTEMA DE CONTROLE DE RIFA')
    print('  Acesse: http://localhost:5000')
    print('=' * 50)
    app.run(debug=True, host='0.0.0.0', port=5000)
