package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/signal"

	"github.com/yym68686/argus/apps/node-host/internal/runtimehost"
)

func main() {
	cfg, err := runtimehost.ParseConfig(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err.Error())
		os.Exit(2)
	}

	host := runtimehost.New(cfg)
	ctx, stop := signal.NotifyContext(context.Background(), shutdownSignals()...)
	defer stop()

	if err := host.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
		fmt.Fprintln(os.Stderr, err.Error())
		os.Exit(1)
	}
}
