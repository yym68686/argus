package ws

import (
	"bufio"
	"crypto/rand"
	"crypto/tls"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/yym68686/argus/apps/node-host-go/internal/util"
)

type Conn struct {
	conn net.Conn
	br   *bufio.Reader

	writeMu sync.Mutex

	pongMu sync.Mutex
	pongCh chan struct{}
}

type DialOptions struct {
	HandshakeTimeout time.Duration
	ConnectTimeout   time.Duration
	TLSConfig        *tls.Config
}

type CloseError struct {
	Code   uint16
	Reason string
}

func (e *CloseError) Error() string {
	if e == nil {
		return "websocket closed"
	}
	if e.Reason != "" {
		return fmt.Sprintf("websocket closed (code=%d reason=%s)", e.Code, e.Reason)
	}
	return fmt.Sprintf("websocket closed (code=%d)", e.Code)
}

func Dial(rawURL string, opt DialOptions) (*Conn, *http.Response, error) {
	u, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil {
		return nil, nil, fmt.Errorf("invalid url: %w", err)
	}
	if u.Scheme != "ws" && u.Scheme != "wss" {
		return nil, nil, fmt.Errorf("unsupported scheme %q (expected ws or wss)", u.Scheme)
	}
	host := u.Host
	if host == "" {
		return nil, nil, errors.New("missing host")
	}
	if !strings.Contains(host, ":") {
		if u.Scheme == "wss" {
			host += ":443"
		} else {
			host += ":80"
		}
	}

	connectTimeout := opt.ConnectTimeout
	if connectTimeout <= 0 {
		connectTimeout = 20 * time.Second
	}
	handshakeTimeout := opt.HandshakeTimeout
	if handshakeTimeout <= 0 {
		handshakeTimeout = connectTimeout
	}

	d := net.Dialer{Timeout: connectTimeout}
	var nc net.Conn
	nc, err = d.Dial("tcp", host)
	if err != nil {
		return nil, nil, err
	}

	if u.Scheme == "wss" {
		cfg := opt.TLSConfig
		if cfg == nil {
			cfg = &tls.Config{}
		} else {
			clone := cfg.Clone()
			cfg = clone
		}
		if cfg.ServerName == "" {
			cfg.ServerName = stripPort(u.Host)
		}
		tlsConn := tls.Client(nc, cfg)
		_ = tlsConn.SetDeadline(time.Now().Add(handshakeTimeout))
		if err := tlsConn.Handshake(); err != nil {
			_ = tlsConn.Close()
			return nil, nil, err
		}
		nc = tlsConn
	}

	_ = nc.SetDeadline(time.Now().Add(handshakeTimeout))

	secKey, err := util.RandBase64(16)
	if err != nil {
		_ = nc.Close()
		return nil, nil, err
	}

	path := u.EscapedPath()
	if path == "" {
		path = "/"
	}
	if u.RawQuery != "" {
		path += "?" + u.RawQuery
	}

	req, _ := http.NewRequest("GET", path, nil)
	req.Host = u.Host
	req.Header.Set("Host", u.Host)
	req.Header.Set("Upgrade", "websocket")
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Sec-WebSocket-Version", "13")
	req.Header.Set("Sec-WebSocket-Key", secKey)
	req.Header.Set("User-Agent", "argus/0.1.0")

	if err := req.Write(nc); err != nil {
		_ = nc.Close()
		return nil, nil, err
	}

	br := bufio.NewReader(nc)
	resp, err := http.ReadResponse(br, req)
	if err != nil {
		_ = nc.Close()
		return nil, nil, err
	}

	if resp.StatusCode != http.StatusSwitchingProtocols {
		_ = nc.Close()
		return nil, resp, fmt.Errorf("unexpected status %d", resp.StatusCode)
	}

	accept := strings.TrimSpace(resp.Header.Get("Sec-WebSocket-Accept"))
	expected := util.WebSocketAcceptKey(secKey)
	if accept == "" || accept != expected {
		_ = nc.Close()
		return nil, resp, errors.New("invalid Sec-WebSocket-Accept")
	}

	_ = nc.SetDeadline(time.Time{})

	c := &Conn{
		conn:   nc,
		br:     br,
		pongCh: make(chan struct{}, 1),
	}
	return c, resp, nil
}

func stripPort(hostport string) string {
	h := hostport
	if strings.Contains(h, "@") {
	}
	if strings.Contains(h, "]") {
		if i := strings.LastIndex(h, "]:"); i != -1 {
			return h[:i+1]
		}
		return h
	}
	if i := strings.LastIndex(h, ":"); i != -1 && i > strings.LastIndex(h, "/") {
		return h[:i]
	}
	return h
}

func (c *Conn) Close() error {
	if c == nil || c.conn == nil {
		return nil
	}
	return c.conn.Close()
}

func (c *Conn) NotifyPong() {
	c.pongMu.Lock()
	defer c.pongMu.Unlock()
	select {
	case c.pongCh <- struct{}{}:
	default:
	}
}

func (c *Conn) WaitPong(timeout time.Duration) bool {
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-c.pongCh:
		return true
	case <-timer.C:
		return false
	}
}

func (c *Conn) WriteText(data []byte, writeTimeout time.Duration) error {
	return c.writeFrame(opText, data, writeTimeout)
}

func (c *Conn) WritePing(payload []byte, writeTimeout time.Duration) error {
	return c.writeFrame(opPing, payload, writeTimeout)
}

func (c *Conn) WritePong(payload []byte, writeTimeout time.Duration) error {
	return c.writeFrame(opPong, payload, writeTimeout)
}

func (c *Conn) WriteClose(code uint16, reason string, writeTimeout time.Duration) error {
	var payload []byte
	if code != 0 {
		payload = make([]byte, 2+len(reason))
		binary.BigEndian.PutUint16(payload[:2], code)
		copy(payload[2:], []byte(reason))
	}
	return c.writeFrame(opClose, payload, writeTimeout)
}

func (c *Conn) writeFrame(opcode byte, payload []byte, writeTimeout time.Duration) error {
	if c == nil || c.conn == nil {
		return errors.New("closed")
	}
	if writeTimeout <= 0 {
		writeTimeout = 10 * time.Second
	}

	c.writeMu.Lock()
	defer c.writeMu.Unlock()

	_ = c.conn.SetWriteDeadline(time.Now().Add(writeTimeout))
	defer c.conn.SetWriteDeadline(time.Time{})

	maskKey := make([]byte, 4)
	if _, err := rand.Read(maskKey); err != nil {
		return err
	}

	header := make([]byte, 0, 2+8+4)
	header = append(header, 0x80|opcode)

	n := len(payload)
	switch {
	case n <= 125:
		header = append(header, 0x80|byte(n))
	case n <= 65535:
		header = append(header, 0x80|126)
		var ext [2]byte
		binary.BigEndian.PutUint16(ext[:], uint16(n))
		header = append(header, ext[:]...)
	default:
		header = append(header, 0x80|127)
		var ext [8]byte
		binary.BigEndian.PutUint64(ext[:], uint64(n))
		header = append(header, ext[:]...)
	}
	header = append(header, maskKey...)

	if _, err := c.conn.Write(header); err != nil {
		return err
	}
	if n == 0 {
		return nil
	}

	masked := make([]byte, n)
	for i := 0; i < n; i++ {
		masked[i] = payload[i] ^ maskKey[i%4]
	}
	_, err := c.conn.Write(masked)
	return err
}

const (
	opCont  = 0x0
	opText  = 0x1
	opBin   = 0x2
	opClose = 0x8
	opPing  = 0x9
	opPong  = 0xA
)

func (c *Conn) ReadMessage(readTimeout time.Duration) (opcode byte, payload []byte, err error) {
	if c == nil || c.conn == nil {
		return 0, nil, errors.New("closed")
	}
	if readTimeout > 0 {
		_ = c.conn.SetReadDeadline(time.Now().Add(readTimeout))
		defer c.conn.SetReadDeadline(time.Time{})
	}

	var msgOpcode byte
	var msgBuf []byte
	var msgIsFragmented bool

	for {
		fin, op, pl, e := c.readFrame()
		if e != nil {
			return 0, nil, e
		}

		switch op {
		case opPing:
			_ = c.WritePong(pl, 5*time.Second)
			continue
		case opPong:
			c.NotifyPong()
			continue
		case opClose:
			code, reason := parseClosePayload(pl)
			_ = c.WriteClose(code, "", 5*time.Second)
			return 0, nil, &CloseError{Code: code, Reason: reason}
		case opText, opBin, opCont:
		default:
			_ = c.WriteClose(1002, "protocol error", 5*time.Second)
			return 0, nil, errors.New("protocol error: unknown opcode")
		}

		if op == opCont {
			if !msgIsFragmented {
				_ = c.WriteClose(1002, "unexpected continuation", 5*time.Second)
				return 0, nil, errors.New("protocol error: unexpected continuation")
			}
			msgBuf = append(msgBuf, pl...)
		} else {
			if msgIsFragmented {
				_ = c.WriteClose(1002, "interleaved fragments", 5*time.Second)
				return 0, nil, errors.New("protocol error: interleaved fragments")
			}
			msgOpcode = op
			msgBuf = append(msgBuf[:0], pl...)
		}

		if fin {
			msgOpcode = msgOpcodeOr(op, msgOpcode)
			return msgOpcode, msgBuf, nil
		}
		msgIsFragmented = true
		msgOpcode = msgOpcodeOr(op, msgOpcode)
	}
}

func msgOpcodeOr(op byte, prev byte) byte {
	if prev != 0 {
		return prev
	}
	if op == opCont {
		return prev
	}
	return op
}

func parseClosePayload(pl []byte) (code uint16, reason string) {
	if len(pl) < 2 {
		return 1000, ""
	}
	code = binary.BigEndian.Uint16(pl[:2])
	reason = string(pl[2:])
	return code, reason
}

func (c *Conn) readFrame() (fin bool, opcode byte, payload []byte, err error) {
	var hdr [2]byte
	if _, err = io.ReadFull(c.br, hdr[:]); err != nil {
		return false, 0, nil, err
	}
	fin = (hdr[0] & 0x80) != 0
	opcode = hdr[0] & 0x0f
	masked := (hdr[1] & 0x80) != 0
	plen7 := int(hdr[1] & 0x7f)

	var plen uint64
	switch plen7 {
	case 126:
		var ext [2]byte
		if _, err = io.ReadFull(c.br, ext[:]); err != nil {
			return false, 0, nil, err
		}
		plen = uint64(binary.BigEndian.Uint16(ext[:]))
	case 127:
		var ext [8]byte
		if _, err = io.ReadFull(c.br, ext[:]); err != nil {
			return false, 0, nil, err
		}
		plen = binary.BigEndian.Uint64(ext[:])
	default:
		plen = uint64(plen7)
	}

	var maskKey [4]byte
	if masked {
		if _, err = io.ReadFull(c.br, maskKey[:]); err != nil {
			return false, 0, nil, err
		}
	}

	if plen > (1<<31 - 1) {
		return false, 0, nil, errors.New("frame too large")
	}
	n := int(plen)
	payload = make([]byte, n)
	if n > 0 {
		if _, err = io.ReadFull(c.br, payload); err != nil {
			return false, 0, nil, err
		}
	}

	if masked {
		for i := 0; i < n; i++ {
			payload[i] ^= maskKey[i%4]
		}
	}
	return fin, opcode, payload, nil
}
