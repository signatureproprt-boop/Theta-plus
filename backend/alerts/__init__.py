"""Phase G — alert package.

    Signal Engine -> Alert Service -> AlertProvider -> Telegram

The signal engine contains NO provider-specific code; it never imports this
package. Alerts are notifications only — no order, no execution.
"""
