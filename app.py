# ============================================
# SISTEMA DE CONTROLE DE RIFA
# Suporte a múltiplas rifas
# ============================================

import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.secret_key = 'rifa-facil-2026'

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
    conn.execute('PRAGMA foreign_keys = ON')  # Habilita chaves estrangeiras
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
            data_criacao TEXT NOT NULL
        )
    ''')

    # Tabela de compras (vinculada à rifa)
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

    conn.commit()
    conn.close()


# =============================
#  ROTAS - RIFAS
# =============================

@app.route('/')
def index():
    """Página inicial - lista todas as rifas cadastradas."""
    conn = get_db()

    # Busca todas as rifas
    rifas = conn.execute('SELECT * FROM rifas ORDER BY data_criacao DESC').fetchall()

    # Conta vendidos por rifa
    stats = {}
    for rifa in rifas:
        vendidos = conn.execute(
            'SELECT COUNT(*) as total FROM compras WHERE rifa_id = ?', (rifa['id'],)
        ).fetchone()['total']
        stats[rifa['id']] = {
            'vendidos': vendidos,
            'disponiveis': rifa['quantidade_numeros'] - vendidos
        }

    conn.close()
    return render_template('index.html', rifas=rifas, stats=stats)


@app.route('/nova-rifa', methods=['POST'])
def nova_rifa():
    """Cria uma nova rifa."""
    nome = request.form.get('nome', '').strip()
    descricao = request.form.get('descricao', '').strip()
    quantidade = request.form.get('quantidade_numeros', type=int)
    valor = request.form.get('valor_numero', type=float) or 0

    # Validações
    if not nome:
        flash('O nome da rifa é obrigatório!', 'erro')
        return redirect(url_for('index'))

    if not quantidade or quantidade < 1 or quantidade > 1000:
        flash('A quantidade deve ser entre 1 e 1000!', 'erro')
        return redirect(url_for('index'))

    conn = get_db()
    data_criacao = datetime.now().strftime('%d/%m/%Y %H:%M')
    conn.execute(
        'INSERT INTO rifas (nome, descricao, quantidade_numeros, valor_numero, data_criacao) VALUES (?, ?, ?, ?, ?)',
        (nome, descricao, quantidade, valor, data_criacao)
    )
    conn.commit()
    conn.close()

    flash(f'Rifa "{nome}" criada com sucesso!', 'sucesso')
    return redirect(url_for('index'))


@app.route('/excluir-rifa/<int:rifa_id>', methods=['POST'])
def excluir_rifa(rifa_id):
    """Exclui uma rifa e todas as suas compras."""
    conn = get_db()
    rifa = conn.execute('SELECT nome FROM rifas WHERE id = ?', (rifa_id,)).fetchone()
    if rifa:
        conn.execute('DELETE FROM compras WHERE rifa_id = ?', (rifa_id,))
        conn.execute('DELETE FROM rifas WHERE id = ?', (rifa_id,))
        conn.commit()
        flash(f'Rifa "{rifa["nome"]}" excluída.', 'sucesso')
    else:
        flash('Rifa não encontrada.', 'erro')
    conn.close()
    return redirect(url_for('index'))


# =============================
#  ROTAS - CARTELA DA RIFA
# =============================

@app.route('/rifa/<int:rifa_id>')
def cartela(rifa_id):
    """Página da cartela individual de uma rifa."""
    conn = get_db()

    # Busca dados da rifa
    rifa = conn.execute('SELECT * FROM rifas WHERE id = ?', (rifa_id,)).fetchone()
    if not rifa:
        flash('Rifa não encontrada.', 'erro')
        conn.close()
        return redirect(url_for('index'))

    # Busca números vendidos desta rifa
    vendidos = conn.execute(
        'SELECT numero, nome, telefone FROM compras WHERE rifa_id = ? ORDER BY numero',
        (rifa_id,)
    ).fetchall()
    conn.close()

    mapa_vendidos = {row['numero']: {'nome': row['nome'], 'telefone': row['telefone']} for row in vendidos}

    return render_template('cartela.html', rifa=rifa, mapa_vendidos=mapa_vendidos)


@app.route('/rifa/<int:rifa_id>/comprar', methods=['POST'])
def comprar(rifa_id):
    """Registra a compra de um ou mais números dentro de uma rifa."""
    numeros_raw = request.form.get('numeros', '').strip()
    nome = request.form.get('nome', '').strip()
    telefone = request.form.get('telefone', '').strip()

    if not numeros_raw or not nome:
        flash('Nome e números são obrigatórios!', 'erro')
        return redirect(url_for('cartela', rifa_id=rifa_id))

    # Converte a string de números para lista de inteiros
    try:
        numeros = [int(n.strip()) for n in numeros_raw.split(',') if n.strip()]
    except ValueError:
        flash('Números inválidos! Use apenas números separados por vírgula.', 'erro')
        return redirect(url_for('cartela', rifa_id=rifa_id))

    if not numeros:
        flash('Selecione ao menos um número.', 'erro')
        return redirect(url_for('cartela', rifa_id=rifa_id))

    conn = get_db()

    # Verifica se a rifa existe
    rifa = conn.execute('SELECT quantidade_numeros FROM rifas WHERE id = ?', (rifa_id,)).fetchone()
    if not rifa:
        flash('Rifa não encontrada.', 'erro')
        conn.close()
        return redirect(url_for('index'))

    # Valida se todos os números pertencem à rifa
    invalidos = [n for n in numeros if n < 1 or n > rifa['quantidade_numeros']]
    if invalidos:
        flash(f'Números fora do intervalo: {", ".join(str(n) for n in invalidos)}', 'erro')
        conn.close()
        return redirect(url_for('cartela', rifa_id=rifa_id))

    # Verifica quais já estão vendidos
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

    # Insere todos os números
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


# --- Inicialização ---
if __name__ == '__main__':
    init_db()
    print('=' * 50)
    print('  SISTEMA DE CONTROLE DE RIFA')
    print('  Acesse: http://localhost:5000')
    print('=' * 50)
    app.run(debug=True, host='0.0.0.0', port=5000)
