"""
Módulo de Conexão SSH Base / Reutilizável usando Paramiko.
Fornece suporte a execução de comandos em equipamentos de rede via SSH Shell.
"""

import time
import re
import logging
import paramiko


class SSHClientSession:
    """
    Gerenciador de sessão SSH interativa via Paramiko Shell.
    """

    def __init__(self, host, username, password, port=22, timeout=30.0, debug=False):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self.debug = debug
        self.client = None
        self.channel = None
        self.session_log_file = None

        if self.debug:
            self.session_log_file = open(f"ssh_session_{host}.log", "w", encoding="utf-8")
            logging.basicConfig(filename=f"ssh_debug_{host}.log", level=logging.DEBUG)
            print(f"[DEBUG] Logs de sessão SSH sendo salvos em 'ssh_session_{host}.log' e 'ssh_debug_{host}.log'")

    def _log(self, data_str):
        if self.session_log_file:
            self.session_log_file.write(data_str)
            self.session_log_file.flush()

    def connect(self):
        """Abre a conexão SSH e inicia a shell."""
        print(f"[*] Conectando via SSH em {self.host}:{self.port}...")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        self.client.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            look_for_keys=False,
            allow_agent=False,
            timeout=self.timeout
        )
        
        self.channel = self.client.invoke_shell(term='dumb', width=500, height=1000)
        self.channel.settimeout(1.0)
        
        # Aguarda prompt inicial
        prompt_buf = ""
        start_time = time.time()
        while time.time() - start_time < 15:
            if self.channel.recv_ready():
                chunk = self.channel.recv(4096).decode('utf-8', errors='ignore')
                prompt_buf += chunk
                self._log(chunk)
                if re.search(r'[#>]', prompt_buf):
                    time.sleep(0.3)
                    break
            time.sleep(0.2)

    def send_command(self, command, expect_regex=r'[\r\n\x1b][<\[]?[^#>\r\n]+[>#]\s*$', timeout=60):
        """Envia um comando para o shell interativo e aguarda a resposta até o prompt."""
        if not command.endswith('\n'):
            command += '\n'

        # Limpa qualquer dado residual do buffer SSH antes de enviar o novo comando
        while self.channel and self.channel.recv_ready():
            try:
                self.channel.recv(16384)
            except Exception:
                break

        self.channel.send(command)
        time.sleep(0.2)
        buf = ""
        cmd_start = time.time()
        cmd_clean = command.strip()
        while time.time() - cmd_start < timeout:
            if self.channel.recv_ready():
                chunk = self.channel.recv(16384).decode('utf-8', errors='ignore')
                buf += chunk
                self._log(chunk)
                if expect_regex and re.search(expect_regex, buf):
                    lines = [l.strip() for l in buf.splitlines() if l.strip()]
                    if len(lines) > 1 or (lines and cmd_clean not in lines[-1]):
                        break
            else:
                time.sleep(0.2)

        return buf

    def exec_command(self, command, timeout=10):
        """
        Executa um comando de forma direta (batch / non-interactive SSH exec).
        Retorna a saída do comando como string.
        """
        if not self.client:
            return ""
        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
            output = stdout.read().decode('utf-8', errors='ignore')
            err = stderr.read().decode('utf-8', errors='ignore')
            result = output or err
            self._log(f"\n--- [EXEC COMMAND: {command}] ---\n{result}\n")
            return result
        except Exception as e:
            return ""

    def close(self):
        """Encerra a sessão SSH."""
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        if self.session_log_file:
            try:
                self.session_log_file.close()
            except Exception:
                pass
