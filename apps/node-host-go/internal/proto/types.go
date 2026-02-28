package proto

type ConnectFrame struct {
	Type        string   `json:"type"`
	NodeID      string   `json:"nodeId"`
	DisplayName string   `json:"displayName"`
	Platform    string   `json:"platform"`
	Version     string   `json:"version"`
	Caps        []string `json:"caps"`
	Commands    []string `json:"commands"`
}

type EventFrame struct {
	Type    string      `json:"type"`
	Event   string      `json:"event"`
	Payload interface{} `json:"payload,omitempty"`
}

type IncomingEventFrame struct {
	Type    string      `json:"type"`
	Event   string      `json:"event"`
	Payload interface{} `json:"payload"`
}

type InvokeRequestPayload struct {
	ID        string      `json:"id"`
	NodeID    string      `json:"nodeId"`
	Command   string      `json:"command"`
	Params    interface{} `json:"params,omitempty"`
	ParamsJSON *string    `json:"paramsJSON"`
	TimeoutMs  *int       `json:"timeoutMs"`
}

type InvokeError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type InvokeResultPayload struct {
	ID          string      `json:"id"`
	NodeID      string      `json:"nodeId"`
	OK          bool        `json:"ok"`
	Payload     interface{} `json:"payload,omitempty"`
	PayloadJSON *string     `json:"payloadJSON"`
	Error       *InvokeError `json:"error"`
}
