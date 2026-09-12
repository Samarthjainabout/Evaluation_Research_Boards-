#include <defs.h>
#include <stub.h>
#include <stdint.h>

// Permanent Caravel firmware shared by scan-debug and native user-area
// Wishbone access. A normal reset with ScanInDL low routes the external scan
// pins to the user project and then idles. A reset with ScanInDL high selects
// WB mode and receives one checked 64-bit runtime command from the FPGA on:
//
//   ScanInDL (GPIO22): pulse-width command data, MSB first
//
// Command bytes: A7, flags(bit0=write), UART tag, WB value[31:0], XOR checksum.
// After command reception both pins are returned to USER input mode, so the
// management core never drives the user project's scan inputs during WB work.

#define REG32(addr) (*(volatile uint32_t *)(addr))
#define BIT(n) (1u << (n))
#define NEURO_ADDR 0x30000004u
#define GPIO_FPGA_READY 1u
#define GPIO_COMMAND_DATA 22u
#define WB_COMMAND_MAGIC 0xA7u
#define WB_UART_SYNC_0 0xA5u
#define WB_UART_SYNC_1 0x5Au
#define WB_READ_SETUP_1 0x00036472u
#define WB_READ_SETUP_2 0x462B000Bu
#define WB_READ_SETUP_3 0x43201405u
#define WB_READ_LEGACY_LOCATION 0x4002AAFFu
#define WB_READ_LEGACY_COMMAND 0x4002AA82u
#define WB_PACKET_MODE_MASK 0xC0000000u
#define WB_PACKET_READ_MODE 0x40000000u
#define WB_PACKET_COL_MASK 0x01F00000u
#define WB_PACKET_COL30 0x01E00000u
#define WB_PACKET_COL31 0x01F00000u
#define WB_READ_POST_ACK_WB_CYCLES 500u
#define WB_READBACK_ATTEMPTS 15u
#define WB_UART_REPLAY_DELAY_CYCLES 100000u
#define WB_COMMAND_EDGE_TIMEOUT 100000u

typedef struct {
    uint8_t write;
    uint8_t tag;
    uint32_t value;
} wb_runtime_command_t;

static uint32_t wb_readbacks[WB_READBACK_ATTEMPTS];
static uint32_t gpio_l_shadow = BIT(GPIO_FPGA_READY);
static uint8_t fpga_ready_level = 1u;

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

static uint8_t command_data(void)
{
    return (reg_mprj_datal & BIT(GPIO_COMMAND_DATA)) != 0u;
}

static void acknowledge_command_bit(void)
{
    fpga_ready_level ^= 1u;
    if (fpga_ready_level) {
        gpio_l_shadow |= BIT(GPIO_FPGA_READY);
    } else {
        gpio_l_shadow &= ~BIT(GPIO_FPGA_READY);
    }
    reg_mprj_datal = gpio_l_shadow;
}

static uint8_t receive_command_byte(uint8_t byte_index)
{
    uint8_t value = 0u;
    static uint32_t one_reference = 0u;
    static uint32_t zero_reference = 0u;

    for (uint8_t bit = 0u; bit < 8u; bit++) {
        uint32_t timeout = 0u;
        uint32_t low_count = 0u;
        uint8_t decoded;

        while (command_data() && timeout < WB_COMMAND_EDGE_TIMEOUT) {
            timeout++;
        }
        // Acknowledge the falling edge immediately.  The FPGA completes the
        // selected pulse width and high separator before it acts on this
        // toggle, while Caravel measures the low interval below.
        if (timeout < WB_COMMAND_EDGE_TIMEOUT) {
            acknowledge_command_bit();
        }
        while (!command_data() && low_count < WB_COMMAND_EDGE_TIMEOUT) {
            low_count++;
        }
        if (timeout >= WB_COMMAND_EDGE_TIMEOUT
            || low_count >= WB_COMMAND_EDGE_TIMEOUT) {
            uint32_t diagnostic = ((uint32_t)byte_index << 24)
                                | ((uint32_t)bit << 16)
                                | (command_data() ? 1u : 0u);
            uart_send_result_frame(0xF3u, diagnostic);
            while (1) {
            }
        }

        // The first two magic bits are known to be 1 then 0.  Use their
        // measured loop counts to calibrate this boot's polling speed, then
        // classify every remaining pulse around the midpoint.  The FPGA does
        // not advance until the ready output toggles below.
        if (byte_index == 0u && bit == 0u) {
            one_reference = low_count;
            decoded = 1u;
        } else if (byte_index == 0u && bit == 1u) {
            zero_reference = low_count;
            decoded = 0u;
        } else {
            decoded = low_count > ((one_reference + zero_reference) >> 1);
        }
        value = (uint8_t)((value << 1) | decoded);
    }
    return value;
}

static wb_runtime_command_t receive_runtime_command(void)
{
    wb_runtime_command_t command;
    uint8_t bytes[8];
    uint8_t checksum = 0u;

    // The FPGA holds data high while Caravel boots, then starts the first
    // clock only after the management pad configuration is complete.
    for (uint8_t i = 0u; i < 8u; i++) {
        bytes[i] = receive_command_byte(i);
    }
    for (uint8_t i = 0u; i < 7u; i++) {
        checksum ^= bytes[i];
    }

    command.write = (uint8_t)(bytes[1] & 1u);
    command.tag = bytes[2];
    command.value = ((uint32_t)bytes[3] << 24)
                  | ((uint32_t)bytes[4] << 16)
                  | ((uint32_t)bytes[5] << 8)
                  | (uint32_t)bytes[6];

    if (bytes[0] != WB_COMMAND_MAGIC || checksum != bytes[7]) {
        uart_send_result_frame(command.tag, 0xBAD0C0DEu);
        while (1) {
        }
    }
    uart_send_result_frame(0xF2u, command.value);
    return command;
}

static void issue_read_setup_once(uint32_t command)
{
    // Preserve the blocking UART pacing used by the verified July firmware.
    print("[TC_READ_HW] Writing command 1: 0x00036472\n");
    REG32(NEURO_ADDR) = WB_READ_SETUP_1;
    wait_cycles(500);

    print("[TC_READ_HW] Writing command 2: 0x462B000B\n");
    REG32(NEURO_ADDR) = WB_READ_SETUP_2;
    wait_cycles(500);

    print("[TC_READ_HW] Writing command 3: 0x43201405\n");
    REG32(NEURO_ADDR) = WB_READ_SETUP_3;
    wait_cycles(500);

    if (command == WB_READ_LEGACY_COMMAND) {
        print("[TC_READ_HW] Writing command 4: 0x4002AAFF\n");
        REG32(NEURO_ADDR) = WB_READ_LEGACY_LOCATION;
        wait_cycles(500);
        print("[TC_READ_HW] Writing command 5: 0x4002AA82\n");
        REG32(NEURO_ADDR) = command;
    } else if ((command & (WB_PACKET_MODE_MASK | WB_PACKET_COL_MASK)) ==
               (WB_PACKET_READ_MODE | WB_PACKET_COL30)) {
        uint32_t paired_col31 = (command & ~WB_PACKET_COL_MASK) | WB_PACKET_COL31;
        print("[TC_READ_HW] Writing command 4: ");
        print_hex32_local(command);
        print("\n");
        REG32(NEURO_ADDR) = command;
        wait_cycles(500);
        print("[TC_READ_HW] Writing command 5: ");
        print_hex32_local(paired_col31);
        print("\n");
        REG32(NEURO_ADDR) = paired_col31;
    } else {
        print("[TC_READ_HW] Writing command 4: ");
        print_hex32_local(command);
        print("\n");
        REG32(NEURO_ADDR) = command;
    }
    wait_cycles(WB_READ_POST_ACK_WB_CYCLES);
    print("[TC_READ_HW] Reading back from 0x30000004\n");
}

static uint32_t perform_readbacks_and_stream(uint8_t base_tag)
{
    uint32_t result = 0u;
    uint32_t first_nonzero = 0u;

    for (uint32_t attempt = 0; attempt < WB_READBACK_ATTEMPTS; attempt++) {
        result = REG32(NEURO_ADDR);
        wb_readbacks[attempt] = result;
        uart_send_result_frame((uint8_t)(base_tag + attempt), result);
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

static void configure_io(uint8_t command_bootstrap)
{
    reg_mprj_io_0 = GPIO_MODE_MGMT_STD_ANALOG;
    reg_mprj_io_1 = GPIO_MODE_MGMT_STD_OUTPUT;
    reg_mprj_io_2 = GPIO_MODE_MGMT_STD_INPUT_NOPULL;
    reg_mprj_io_3 = GPIO_MODE_MGMT_STD_INPUT_NOPULL;
    reg_mprj_io_4 = GPIO_MODE_MGMT_STD_INPUT_NOPULL;
    reg_mprj_io_5 = GPIO_MODE_MGMT_STD_INPUT_NOPULL;
    reg_mprj_io_6 = GPIO_MODE_MGMT_STD_OUTPUT;  // UART TX -> FPGA J10-10
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
    reg_mprj_io_21 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // ScanInDR
    reg_mprj_io_22 = command_bootstrap
        ? GPIO_MODE_MGMT_STD_INPUT_NOPULL
        : GPIO_MODE_USER_STD_INPUT_NOPULL;             // ScanInDL / command data
    reg_mprj_io_23 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_24 = GPIO_MODE_USER_STD_INPUT_NOPULL;
    reg_mprj_io_25 = GPIO_MODE_USER_STD_ANALOG;  // dc_bias
    reg_mprj_io_26 = GPIO_MODE_USER_STD_ANALOG;  // Vcc_wl_read
    reg_mprj_io_27 = GPIO_MODE_USER_STD_ANALOG;  // Vcc_set
    reg_mprj_io_28 = GPIO_MODE_USER_STD_ANALOG;  // Vcc_wl_reset
    reg_mprj_io_29 = GPIO_MODE_USER_STD_ANALOG;  // Vbias
    reg_mprj_io_30 = GPIO_MODE_USER_STD_ANALOG;  // Vcc_wl_set
    reg_mprj_io_31 = GPIO_MODE_USER_STD_ANALOG;  // Bias_comp2
    reg_mprj_io_32 = GPIO_MODE_USER_STD_ANALOG;  // Vcomp
    reg_mprj_io_33 = GPIO_MODE_USER_STD_ANALOG;  // Vcc_read
    reg_mprj_io_34 = GPIO_MODE_USER_STD_ANALOG;  // Iref
    reg_mprj_io_35 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // ScanInCC
    reg_mprj_io_36 = GPIO_MODE_USER_STD_INPUT_NOPULL;  // TM
    reg_mprj_io_37 = GPIO_MODE_USER_STD_INPUT_NOPULL;

    reg_mprj_xfer = 1;
    while (reg_mprj_xfer == 1) {
    }
    gpio_l_shadow = BIT(GPIO_FPGA_READY);
    fpga_ready_level = 1u;
    reg_mprj_datal = gpio_l_shadow;
}

static void scan_debug_idle(void)
{
    print("[PERMANENT] scan-debug routing active\n");
    while (1) {
    }
}

void main(void)
{
    wb_runtime_command_t command;
    uint32_t result;

    reg_gpio_mode1 = 1;
    reg_gpio_mode0 = 0;
    reg_gpio_ien = 1;
    reg_gpio_oe = 1;
    reg_gpio_out = 0;
    configure_io(1u);
    reg_uart_enable = 1;
    reg_wb_enable = 1;
    wait_cycles(1000);
    reg_gpio_out = 1;
    uart_send_result_frame(0xF0u, command_data() ? 1u : 0u);

    if (!command_data()) {
        configure_io(0u);
        scan_debug_idle();
    }

    uart_send_result_frame(0xF1u, 1u);
    command = receive_runtime_command();
    configure_io(0u);

    print("[PERMANENT] operation=");
    print(command.write ? "write value=" : "read value=");
    print_hex32_local(command.value);
    print("\n");

    if (command.write) {
        REG32(NEURO_ADDR) = command.value;
        result = command.value;
        uart_send_result_frame(command.tag, result);
        print("[WB_GUI_WRITE] address=0x30000004 value=");
        print_hex32_local(result);
        print("\nWB_GUI_DONE\n");
        while (1) {
            wait_cycles(1000000);
            uart_send_result_frame(command.tag, result);
        }
    }

    issue_read_setup_once(command.value);
    result = perform_readbacks_and_stream(command.tag);
    print("[WB_GUI_READ] command=");
    print_hex32_local(command.value);
    print(" address=0x30000004 value=");
    print_hex32_local(result);
    print("\nWB_GUI_DONE\n");

    while (1) {
        for (uint32_t attempt = 0; attempt < WB_READBACK_ATTEMPTS; attempt++) {
            wait_cycles(WB_UART_REPLAY_DELAY_CYCLES);
            uart_send_result_frame((uint8_t)(command.tag + attempt), wb_readbacks[attempt]);
        }
    }
}
