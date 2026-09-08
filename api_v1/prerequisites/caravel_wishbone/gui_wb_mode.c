#include <defs.h>
#include <stub.h>
#include <stdint.h>

// Native user-area Wishbone access firmware used by the GUI.
// The build selects one operation with WB_OPERATION_WRITE and, for writes,
// supplies WB_WRITE_VALUE.  The target address remains the established
// neuromorphic user register used by the supplied WB firmware modes.

#define REG32(addr) (*(volatile uint32_t *)(addr))
#define NEURO_ADDR 0x30000004u
#define WB_UART_SYNC_0 0xA5u
#define WB_UART_SYNC_1 0x5Au
#ifndef WB_UART_TAG_READ
#define WB_UART_TAG_READ 0x52u
#endif
#ifndef WB_UART_TAG_WRITE
#define WB_UART_TAG_WRITE 0x57u
#endif
#define WB_READ_SETUP_1 0x00036472u
#define WB_READ_SETUP_2 0x462B000Bu
#define WB_READ_SETUP_3 0x43201405u
#define WB_READ_LEGACY_LOCATION 0x4002AAFFu
#define WB_READ_R31C31 0x7FF2AA82u
#define WB_READ_POST_ACK_WB_CYCLES 500u
#define WB_READBACK_ATTEMPTS 15u
#define WB_UART_REPLAY_DELAY_CYCLES 100000u

static uint32_t wb_readbacks[WB_READBACK_ATTEMPTS];

#ifndef WB_OPERATION_WRITE
#define WB_OPERATION_WRITE 0
#endif

#ifndef WB_WRITE_VALUE
#define WB_WRITE_VALUE 0x500888FFu
#endif

static inline void wait_cycles(uint32_t cycles)
{
    for (volatile uint32_t i = 0; i < cycles; i++) {
        __asm__ volatile ("nop");
    }
}

static void print_hex32_local(uint32_t value)
{
    static const char hex[] = "0123456789ABCDEF";
    char buf[11];

    buf[0] = '0';
    buf[1] = 'x';
    for (int i = 0; i < 8; i++) {
        buf[2 + i] = hex[(value >> (28 - 4 * i)) & 0xFu];
    }
    buf[10] = '\0';
    print(buf);
}

static void uart_put_raw(uint8_t value)
{
    while (reg_uart_txfull == 1) {
    }
    reg_uart_data = value;
}

static void uart_send_result_frame(uint8_t tag, uint32_t value)
{
    uint8_t b3 = (uint8_t)(value >> 24);
    uint8_t b2 = (uint8_t)(value >> 16);
    uint8_t b1 = (uint8_t)(value >> 8);
    uint8_t b0 = (uint8_t)value;
    uint8_t checksum = tag ^ b3 ^ b2 ^ b1 ^ b0;

    uart_put_raw(WB_UART_SYNC_0);
    uart_put_raw(WB_UART_SYNC_1);
    uart_put_raw(tag);
    uart_put_raw(b3);
    uart_put_raw(b2);
    uart_put_raw(b1);
    uart_put_raw(b0);
    uart_put_raw(checksum);
}

static void issue_read_setup_once(void)
{
    // Preserve the blocking UART pacing used by the July hardware firmware.
    // At 9600 baud these messages provide substantial settling time between
    // consecutive writes in addition to the original 500-cycle delay.
    print("[TC_READ_HW] Writing command 1: 0x00036472\n");
    REG32(NEURO_ADDR) = WB_READ_SETUP_1;
    wait_cycles(500);

    print("[TC_READ_HW] Writing command 2: 0x462B000B\n");
    REG32(NEURO_ADDR) = WB_READ_SETUP_2;
    wait_cycles(500);

    print("[TC_READ_HW] Writing command 3: 0x43201405\n");
    REG32(NEURO_ADDR) = WB_READ_SETUP_3;
    wait_cycles(500);

#if WB_WRITE_VALUE == 0x4002AA82u
    // The verified nonzero legacy run selected this location before issuing
    // its final read command. Keep it scoped to the matching legacy packet.
    print("[TC_READ_HW] Writing command 4: 0x4002AAFF\n");
    REG32(NEURO_ADDR) = WB_READ_LEGACY_LOCATION;
    wait_cycles(500);

    print("[TC_READ_HW] Writing command 5: ");
#elif WB_WRITE_VALUE == 0x7FE2AA82u
    // Select r31c30 first, then issue r31c31 as the final read command.
    print("[TC_READ_HW] Writing command 4: 0x7FE2AA82\n");
    REG32(NEURO_ADDR) = (uint32_t)WB_WRITE_VALUE;
    wait_cycles(500);

    print("[TC_READ_HW] Writing command 5: 0x7FF2AA82\n");
    REG32(NEURO_ADDR) = WB_READ_R31C31;
    wait_cycles(WB_READ_POST_ACK_WB_CYCLES);
#else
    print("[TC_READ_HW] Writing command 4: ");
#endif
#if WB_WRITE_VALUE != 0x7FE2AA82u
    print_hex32_local((uint32_t)WB_WRITE_VALUE);
    print("\n");
    REG32(NEURO_ADDR) = (uint32_t)WB_WRITE_VALUE;
    wait_cycles(WB_READ_POST_ACK_WB_CYCLES);
#endif

    print("[TC_READ_HW] Reading back from 0x30000004\n");
}

static uint32_t perform_readbacks_and_stream(void)
{
    uint32_t result = 0u;
    uint32_t first_nonzero = 0u;

    for (uint32_t attempt = 0; attempt < WB_READBACK_ATTEMPTS; attempt++) {
        result = REG32(NEURO_ADDR);
        wb_readbacks[attempt] = result;
        uart_send_result_frame((uint8_t)(WB_UART_TAG_READ + attempt), result);
        print("[WB_GUI_READ] readback=");
        print_hex32_local(result);
        print("\n");
        if (first_nonzero == 0u && result != 0u) {
            first_nonzero = result;
        }
        wait_cycles(500);
    }
    return first_nonzero;
}

static void configure_io(void)
{
    // Preserve the GPIO modes from the manually verified WB firmware.
    reg_mprj_io_0 = GPIO_MODE_MGMT_STD_ANALOG;
    reg_mprj_io_1 = GPIO_MODE_MGMT_STD_OUTPUT;
    reg_mprj_io_2 = GPIO_MODE_MGMT_STD_INPUT_NOPULL;
    reg_mprj_io_3 = GPIO_MODE_MGMT_STD_INPUT_NOPULL;
    reg_mprj_io_4 = GPIO_MODE_MGMT_STD_INPUT_NOPULL;
    reg_mprj_io_5 = GPIO_MODE_MGMT_STD_INPUT_NOPULL;  // UART RX: unused in WB mode
    reg_mprj_io_6 = GPIO_MODE_MGMT_STD_OUTPUT;        // UART TX -> FPGA J10-10
    reg_mprj_io_7 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_8 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_9 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_10 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_11 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_12 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_13 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_14 = GPIO_MODE_MGMT_STD_OUTPUT;
    reg_mprj_io_15 = GPIO_MODE_MGMT_STD_OUTPUT;
    reg_mprj_io_16 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_17 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_18 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_19 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_20 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    // External scan/test controls are driven by the FPGA.  Keep them as
    // Caravel user inputs in every WB run; driving them here would contend
    // with the FPGA.  WB mode holds the external values at their idle levels.
    reg_mprj_io_21 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // ScanInDR: FPGA holds high
    reg_mprj_io_22 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // ScanInDL: FPGA holds low
    reg_mprj_io_23 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_24 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    // Controlled legacy-mode trial: match the old successful WB firmware by
    // leaving the externally biased pads as user digital inputs with no pull.
    reg_mprj_io_25 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // dc_bias pad
    reg_mprj_io_26 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // Vcc_wl_read pad
    reg_mprj_io_27 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // Vcc_set pad
    reg_mprj_io_28 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // Vcc_wl_reset pad
    reg_mprj_io_29 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // Vbias pad
    reg_mprj_io_30 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // Vcc_wl_set pad
    reg_mprj_io_31 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // Bias_comp2 pad
    reg_mprj_io_32 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // Vcomp pad
    reg_mprj_io_33 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // Vcc_read pad
    reg_mprj_io_34 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // Iref pad
    reg_mprj_io_35 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // ScanInCC: FPGA holds low
    reg_mprj_io_36 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_37 = GPIO_MODE_USER_STD_INPUT_NOPULL;

    reg_mprj_xfer = 1;
    while (reg_mprj_xfer == 1) {
    }
}

void main(void)
{
    volatile uint32_t result;

    reg_gpio_mode1 = 1;
    reg_gpio_mode0 = 0;
    reg_gpio_ien = 1;
    reg_gpio_oe = 1;
    reg_gpio_out = 0;

    configure_io();
    reg_uart_enable = 1;
    print("\n[TC_READ_HW] firmware start\n");
    reg_wb_enable = 1;
    wait_cycles(1000);
    reg_gpio_out = 1;
    print("[TC_READ_HW] Wishbone enabled\n");

#if WB_OPERATION_WRITE
    print("[WB_GUI_WRITE] Writing command: ");
    print_hex32_local((uint32_t)WB_WRITE_VALUE);
    print("\n");
    REG32(NEURO_ADDR) = (uint32_t)WB_WRITE_VALUE;
    result = (uint32_t)WB_WRITE_VALUE;
    uart_send_result_frame(WB_UART_TAG_WRITE, result);
    print("[WB_GUI_WRITE] address=0x30000004 value=");
#else
    issue_read_setup_once();
    result = perform_readbacks_and_stream();
    print("[WB_GUI_READ] setup=remote_read_mode_wb command=");
    print_hex32_local((uint32_t)WB_WRITE_VALUE);
    print(" address=0x30000004 value=");
#endif
    print_hex32_local(result);
    print("\nWB_GUI_DONE\n");

#if WB_OPERATION_WRITE
    while (1) {
        wait_cycles(1000000);
            uart_send_result_frame(WB_UART_TAG_WRITE, result);
            print("[WB_GUI_WRITE] value=");
        print_hex32_local(result);
        print("\nWB_GUI_DONE\n");
    }
#else
    // Replay the captured batch with one tag per attempt. This lets the FPGA
    // VIO collector recover all 15 values even after the reset command's
    // Vivado session exits. No setup or Wishbone read is repeated here.
    while (1) {
        for (uint32_t attempt = 0; attempt < WB_READBACK_ATTEMPTS; attempt++) {
            wait_cycles(WB_UART_REPLAY_DELAY_CYCLES);
            uart_send_result_frame((uint8_t)(WB_UART_TAG_READ + attempt), wb_readbacks[attempt]);
            print("[WB_GUI_READ] replay=");
            print_hex32_local(wb_readbacks[attempt]);
            print("\n");
        }
    }
#endif
}
