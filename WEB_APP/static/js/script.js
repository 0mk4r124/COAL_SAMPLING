let currentPage = 1;
let scale = 1;
let isDragging = false;
let startX, startY, imgX = 0, imgY = 0;
let currentOpenedAnodeId = null;
let currentOpenedBunch = null;
let isImageLoading = false;
let modalOpen = false;

// State management variables
let currentUID = null;
let vehicleDetailsModalOpen = false;
let emergencyModalOpen = false;
let autoManualModalOpen = false;
let statusCheckInterval = null;

// Print job state
let pendingPrintRow     = null;
let printCountdownTimer = null;
let printModalInstance  = null;

const img = document.getElementById("modalImage");
const wrapper = document.getElementById("zoomWrapper");
const modalElement = document.getElementById('vehicleDetailsModal');
const vehicleModal = new bootstrap.Modal(modalElement);

function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== "") {
        const cookies = document.cookie.split(";");
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + "=")) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

const csrftoken = getCookie("csrftoken");

// ════════════════════════════════════════════════════════════════════════════
// STATE AND STATUS MANAGEMENT
// ════════════════════════════════════════════════════════════════════════════

let waitModalInstance = null;
let waitModalOpen = false;

function handleBlockingState(data) {
    const modalEl = document.getElementById('waitModal');

    if (!waitModalInstance) {
        waitModalInstance = new bootstrap.Modal(modalEl, {
            backdrop: 'static',
            keyboard: false
        });
    }

    if (data.status === "blocked") {
        if (!waitModalOpen) {
            waitModalOpen = true;
function fetchVehicleMaster() {
    fetch('/api/vehicle_master/')
        .then(res => res.json())
        .then(data => {
            const tbody = document.getElementById('vehicleMasterBody');
            tbody.innerHTML = '';

            data.data.forEach(row => {
                const tr = document.createElement('tr');

                tr.innerHTML = `
                    <td>${row.rfid}</td>
                    <td>${row.vehicle_number}</td>
                    <td>${row.vendor_name}</td>
                    <td>${row.vendor_code}</td>
                    <td>
                        <button class="btn btn-sm btn-warning"
                            onclick='openEditModal(${JSON.stringify(row)})'>
                            Edit
                        </button>
                    </td>
                `;

                tbody.appendChild(tr);
            });
        });
}
            const message = document.getElementById('waitModalMessage');

            if (data.reason === "EMERGENCY_ACTIVE") {
                message.textContent = "Emergency is ACTIVE. Waiting until it is cleared...";
            } 
            else if (data.reason === "AUTO_MANUAL_ACTIVE") {
                message.textContent = "Manual mode is ACTIVE. Waiting until it is cleared...";
            } 
            else {
                message.textContent = "System is blocked. Waiting...";
            }

            waitModalInstance.show();
        }

        return true; // block further UI updates
    }

    // ✅ HIDE MODAL when cleared
    if (waitModalOpen) {
        waitModalOpen = false;
        waitModalInstance.hide();
    }

    return false;
}

document.getElementById('vendorNameInput').addEventListener('input', function () {

    const inputValue = this.value.trim().toLowerCase();

    const vendorCodeInput = document.getElementById('vendorCodeInput');
    // const bucketInput = document.getElementById('bucketNoInput');

    // Try to find match
    const match = window.vendorCache.find(v => 
        v.vendor_name.toLowerCase() === inputValue
    );

    if (match) {
        // ✅ Existing vendor → autofill
        vendorCodeInput.value = match.vendor_code || '';
        // bucketInput.value = match.bucket_no || '';

        vendorCodeInput.readOnly = true;
        // bucketInput.readOnly = true;
    } else {
        // ✅ New vendor → allow input
        vendorCodeInput.value = '';
        // bucketInput.value = '';

        vendorCodeInput.readOnly = false;
        // bucketInput.readOnly = false;
    }
});

document.getElementById("vehicleNumberInput").addEventListener("input", function() {
    this.value = this.value.toUpperCase();
});

document.getElementById('vehicleDetailsModal').addEventListener('hidden.bs.modal', function () {
    vehicleDetailsModalOpen = false;
});

function updateCurrentStatus() {
    "use strict";
    fetch('/api/get_current_status/')
        .then(response => response.json())
        .then(data => {
            // Update state display

            if (handleBlockingState(data)) {
                return; // stop everything else
            }

            if (data.add_vehicle === "YES" && !vehicleDetailsModalOpen) {

                const input = document.getElementById('rfidInput');
                input.value = data.rfids || '';
                input.disabled = true;

                window.vendorCache = data.vendors || [];
                const datalist = document.getElementById('vendorList');
                datalist.innerHTML = '';
                window.vendorCache.forEach(v => {
                    const opt = document.createElement('option');
                    opt.value = v.vendor_name;
                    datalist.appendChild(opt);
                });
            
                vehicleDetailsModalOpen = true;
                const modal = new bootstrap.Modal(document.getElementById('vehicleDetailsModal'));
                modal.show();
            }

            document.getElementById('currentState').textContent = data.current_state || 'IDLE';
            document.getElementById('dashVendorName').textContent = data.vendor_name || 'NOT FOUND';
            document.getElementById('dashVehicleNumber').textContent = data.vehicle_number || 'NOT FOUND';
            
            currentUID = data.uid;

            // Retake button state
            const retakeBtn = document.getElementById('retakeBtn');
            if (retakeBtn) {
                retakeBtn.disabled = !data.retake_available;
            }
            lastErrorUID = data.last_error_uid || null;
            
        })
        .catch(err => console.error("Status update failed:", err));
}

function resetSystemConfirm() {
    const resetModal = new bootstrap.Modal(document.getElementById('resetConfirmModal'));
    resetModal.show();
}

function confirmResetSystem() {
    if (!currentUID) {
        alert('No active vehicle. Cannot reset.');
        return;
    }
    
    fetch('/api/reset_system/', {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrftoken,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ uid: currentUID })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            bootstrap.Modal.getInstance(document.getElementById('resetConfirmModal')).hide();
            alert('System has been reset. All processes marked as ERROR. Ready for next vehicle.');
            // Refresh status
            updateCurrentStatus();
        } else {
            alert('Error: ' + (res.error || 'Unknown error'));
        }
    })
    .catch(err => {
        console.error(err);
        alert('Error resetting system');
    });
}

// Reset zoom & position whenever modal opens
document.getElementById("imageModal").addEventListener("shown.bs.modal", () => {
    scale = 1;
    imgX = imgY = 0;
    img.style.transform = `translate(0px, 0px) scale(1)`;
});

// Mouse wheel zoom
wrapper.addEventListener("wheel", (e) => {
    e.preventDefault();
    const delta = e.deltaY < 0 ? 0.1 : -0.1;
    scale = Math.min(Math.max(0.5, scale + delta), 4); // limit zoom 0.5x – 4x
    img.style.transform = `translate(${imgX}px, ${imgY}px) scale(${scale})`;
});

// Drag image
wrapper.addEventListener("mousedown", (e) => {
    isDragging = true;
    wrapper.style.cursor = "grabbing";
    startX = e.clientX - imgX;
    startY = e.clientY - imgY;
});

wrapper.addEventListener("mousemove", (e) => {
    if (!isDragging) return;
    imgX = e.clientX - startX;
    imgY = e.clientY - startY;
    img.style.transform = `translate(${imgX}px, ${imgY}px) scale(${scale})`;
});

wrapper.addEventListener("mouseup", () => {
    isDragging = false;
    wrapper.style.cursor = "grab";
});

wrapper.addEventListener("mouseleave", () => {
    isDragging = false;
});

function downloadHistoryData() {
    const dateRange = document.getElementById('date_range').value;

    if (!dateRange) {
        alert("Please select a date range");
        return;
    }

    let startDate, endDate;

    if (dateRange.includes(" - ")) {
        [startDate, endDate] = dateRange.split(" - ").map(s => s.trim());
    } else {
        startDate = endDate = dateRange.trim();
    }

    const vehicleNumber = document.getElementById('vehicle_number_search').value.trim();
    const vendorName = document.getElementById('vendor_name_search').value.trim();

    let url = `/api/download_history_data/?start_date=${startDate}&end_date=${endDate}`;

    if (vehicleNumber) {
        url += `&vehicle_number=${encodeURIComponent(vehicleNumber)}`;
    }

    if (vendorName) {
        url += `&vendor_name=${encodeURIComponent(vendorName)}`;
    }

    window.open(url, '_blank');
}

function sendPrintData(row) {
    pendingPrintRow = row;

    // Reset modal to initial state
    const footer    = document.getElementById('printModalFooter');
    const countdown = document.getElementById('printCountdownSection');
    const btn       = document.getElementById('confirmPrintBtn');

    footer.style.display    = '';
    countdown.style.display = 'none';
    btn.disabled            = false;
    btn.innerHTML           = '<i class="fas fa-print"></i> Send Print';

    document.getElementById('printConfirmText').innerHTML =
        `Send print job for: <strong>${row.vehicle_number}</strong> &mdash; ${row.vendor_name}<br>
         <small class="text-muted">${row.datetimestamp}</small>`;

    if (!printModalInstance) {
        printModalInstance = new bootstrap.Modal(
            document.getElementById('printConfirmModal'),
            { backdrop: 'static', keyboard: false }
        );
    }
    printModalInstance.show();
}

function confirmSendPrint() {
    if (!pendingPrintRow) return;

    const row = pendingPrintRow;
    const btn = document.getElementById('confirmPrintBtn');
    btn.disabled  = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Sending...';

    fetch('/api/send_print_data/', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrftoken, 'Content-Type': 'application/json' },
        body: JSON.stringify({
            vehicle_number: row.vehicle_number,
            vendor_name:    row.vendor_name,
            dtstamp:        row.datetimestamp
        })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            document.getElementById('printModalFooter').style.display = 'none';
            document.getElementById('printCountdownSection').style.display = '';
            startPrintCountdown(120);
        } else {
            btn.disabled  = false;
            btn.innerHTML = '<i class="fas fa-print"></i> Send Print';
            alert('❌ Print failed: ' + (res.error || 'Unknown error'));
        }
    })
    .catch(err => {
        btn.disabled  = false;
        btn.innerHTML = '<i class="fas fa-print"></i> Send Print';
        console.error(err);
        alert('❌ Network error while sending print job');
    });
}

function startPrintCountdown(seconds) {
    let remaining = seconds;
    document.getElementById('printCountdownDisplay').textContent = remaining;
    clearInterval(printCountdownTimer);
    printCountdownTimer = setInterval(() => {
        remaining--;
        document.getElementById('printCountdownDisplay').textContent = remaining;
        if (remaining <= 0) {
            clearInterval(printCountdownTimer);
            sendStopPrint();
        }
    }, 1000);
}

function stopPrintEarly() {
    clearInterval(printCountdownTimer);
    sendStopPrint();
}

function sendStopPrint() {
    fetch('/api/stop_print_job/', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrftoken, 'Content-Type': 'application/json' },
        body: JSON.stringify({})
    })
    .then(r => r.json())
    .then(() => { if (printModalInstance) printModalInstance.hide(); })
    .catch(err => {
        console.error(err);
        if (printModalInstance) printModalInstance.hide();
    });
}

// ════════════════════════════════════════════════════════════════════════════
// RETAKE FAILED CYCLE
// ════════════════════════════════════════════════════════════════════════════

let lastErrorUID        = null;
let retakeModalInstance = null;

function retakeCycle() {
    if (!lastErrorUID) {
        alert('No failed cycle available to retake.');
        return;
    }
    if (!retakeModalInstance) {
        retakeModalInstance = new bootstrap.Modal(
            document.getElementById('retakeConfirmModal'),
            { backdrop: 'static', keyboard: false }
        );
    }
    retakeModalInstance.show();
}

function confirmRetakeCycle() {
    if (!lastErrorUID) return;

    const btn = document.getElementById('confirmRetakeBtn');
    btn.disabled  = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Starting...';

    fetch('/api/retake_failed_cycle/', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrftoken, 'Content-Type': 'application/json' },
        body: JSON.stringify({ uid: lastErrorUID })
    })
    .then(r => r.json())
    .then(res => {
        btn.disabled  = false;
        btn.innerHTML = '<i class="fas fa-redo"></i> Yes, Retake';
        if (res.success) {
            retakeModalInstance.hide();
            alert('✅ Retake cycle started.\nNew session: ' + res.new_uid);
        } else {
            alert('❌ Retake failed: ' + (res.error || 'Unknown error'));
        }
    })
    .catch(err => {
        btn.disabled  = false;
        btn.innerHTML = '<i class="fas fa-redo"></i> Yes, Retake';
        console.error(err);
        alert('❌ Network error during retake');
    });
}

function fetchVehicleMaster(page=1) {
    fetch(`/api/vehicle_master/?page=${page}`)
        .then(res => res.json())
        .then(data => {
            const tbody = document.getElementById('vehicleMasterBody');
            tbody.innerHTML = '';

            data.data.forEach(row => {
                const tr = document.createElement('tr');

                tr.innerHTML = `
                    <td>${row.sno}</td>
                    <td>${row.rfid}</td>
                    <td>${row.vehicle_number}</td>
                    <td>${row.vendor_name}</td>
                    <td>${row.vendor_code}</td>
                    <td>
                        <button class="btn btn-sm btn-warning"
                            onclick='openEditModal(${JSON.stringify(row)})'>
                            Edit
                        </button>
                    </td>
                `;

                tbody.appendChild(tr);
            });

            setupPagination(
                data.total,
                data.page,
                data.per_page,
                "vehicle_master"
            );
        });
}

document.addEventListener("DOMContentLoaded", function () {
    // Initialize daterangepicker with default last 7 days
    $('#date_range').daterangepicker({
        autoUpdateInput: true,
        startDate: moment().subtract(0, 'days'),
        endDate: moment(),
        locale: {
            format: "YYYY-MM-DD",
            cancelLabel: "Clear",
            applyLabel: "Apply"
        },
        opens: "right",
        drops: "down",
        autoApply: false,
        buttonClasses: ['btn', 'btn-sm', 'btn-primary'],
    }, function (start, end) {
        $('#date_range').val(start.format("YYYY-MM-DD") + " - " + end.format("YYYY-MM-DD"));
    });

    // Apply event
    $('#date_range').on("apply.daterangepicker", function (ev, picker) {
        const start = picker.startDate.format("YYYY-MM-DD");
        const end = picker.endDate.format("YYYY-MM-DD");
        $(this).val(start === end ? start : start + " - " + end);
    });

    // Cancel event
    $('#date_range').on("cancel.daterangepicker", function () {
        $(this).val("");
    });
});

function fetchHistoryData(page=1) {
    const dateRange = document.getElementById('date_range').value;
    if (!dateRange) {
        alert("Please select a date range");
        return;
    }
    if (dateRange.includes(" - ")) {
        [startDate, endDate] = dateRange.split(" - ").map(s => s.trim());
    } else {
        startDate = endDate = dateRange.trim();
    }
    
    const vehicleNumber = document.getElementById('vehicle_number_search').value.trim();
    const vendorNumber = document.getElementById('vendor_name_search').value.trim();
    let url = `/api/fetch_history_data/?start_date=${startDate}&end_date=${endDate}&page=${page}`;
    if (vehicleNumber) {
        url += `&vehicle_number=${encodeURIComponent(vehicleNumber)}`;
    }
    if (vendorNumber) {
        url += `&vendor_name=${encodeURIComponent(vendorNumber)}`;
    }
    
    fetch(url)
        .then(response => response.json())
        .then(data => {
            populateTable(data.data, 'History_body');
            setupPagination(data.total, data.page, data.per_page, "history");
        })
        .catch(err => console.error(err));
}

function populateTable(data, table_name) {
    const tbody = document.getElementById(table_name);
    tbody.innerHTML = '';

    if (table_name === 'History_body'){
        data.forEach(row => {
            const tr = document.createElement('tr');
            const VehicleImgButton = row.vehicle_image
                ? `<button class="btn btn-sm btn-primary view-image-btn" data-timestamp="${row.create_time}" data-vehicle_number="${row.vehicle_number}" data-src="${row.vehicle_image}">Vehicle View</button>`
                : "";
            const Img1Button = row.sample_1_image
                ? `<button class="btn btn-sm btn-primary view-image-btn" data-timestamp="${row.create_time}" data-vehicle_number="${row.vehicle_number}" data-src="${row.sample_1_image}">Sample View</button>`
                : "";
            const Img2Button = row.sample_2_image
                ? `<button class="btn btn-sm btn-primary view-image-btn" data-timestamp="${row.create_time}" data-vehicle_number="${row.vehicle_number}" data-src="${row.sample_2_image}">Sample View</button>`
                : "";
            const Img3Button = row.sample_3_image
                ? `<button class="btn btn-sm btn-primary view-image-btn" data-timestamp="${row.create_time}" data-vehicle_number="${row.vehicle_number}" data-src="${row.sample_3_image}">Sample View</button>`
                : "";
            const ReportButton = row.report_path
                ? `<button class="btn btn-sm btn-primary" onclick="window.open('/api/serve-file/?file=${encodeURIComponent(row.report_path)}', '_blank')">Report</button>`
                : "";
            const PrintButton = `<button class="btn btn-sm btn-warning" onclick="sendPrintData(${JSON.stringify(row).replace(/"/g, '&quot;')})">
                <i class="fas fa-print"></i> Print
            </button>`;

            tr.innerHTML = `
                <td>${row.sno}</td>
                <td>${row.datetimestamp}</td>
                <td>${row.vehicle_number}</td>
                <td>${row.vendor_name}</td>
                <td>${row.vendor_code}</td>
                <td>${row.bucket_no}</td>
                <td>${VehicleImgButton}</td>
                <td>${Img1Button}</td>
                <td>${Img2Button}</td>
                <td>${Img3Button}</td>
                <td>${ReportButton}</td>
                <td>${PrintButton}</td>
            `;
            tbody.appendChild(tr);
        });
    }
}

function setupPagination(total, current, perPage, tab) {
    const totalPages = Math.ceil(total / perPage);

    const pagination = {
        total_records: total,
        current_page: current,
        has_previous: current > 1,
        has_next: current < totalPages,
        start_index: (current - 1) * perPage + 1,
        end_index: Math.min(current * perPage, total),
        custom_page_range: getCustomPageRange(current, totalPages)
    };

    renderPagination(pagination, tab);
}

function getCustomPageRange(current, totalPages) {
    const delta = 2;
    const range = [];
    for (let i = Math.max(1, current - delta); i <= Math.min(totalPages, current + delta); i++) {
        range.push(i);
    }
    if (range[0] > 1) {
        if (range[0] > 2) range.unshift('...');
        range.unshift(1);
    }
    if (range[range.length - 1] < totalPages) {
        if (range[range.length - 1] < totalPages - 1) range.push('...');
        range.push(totalPages);
    }
    return range;
}

function renderPagination(pagination, tab) {
    const container = document.getElementById(`pagination-${tab}`);
    if (!pagination) return;

    let html = `
        <div>
            <p class="mb-0 text-muted small">
                Showing ${pagination.start_index}–${pagination.end_index} of ${pagination.total_records} entries
            </p>
        </div>

        <div>
            <nav>
                <ul class="pagination mb-0">
                ${pagination.has_previous ?
                    `<li class="page-item"><a class="page-link" href="#" data-page="${pagination.current_page - 1}">Previous</a></li>` :
                    `<li class="page-item disabled"><span class="page-link">Previous</span></li>`}
    `;

    pagination.custom_page_range.forEach(num => {
        if (num === '...') {
            html += `<li class="page-item disabled"><span class="page-link">…</span></li>`;
        } else if (num === pagination.current_page) {
            html += `<li class="page-item active"><span class="page-link">${num}</span></li>`;
        } else {
            html += `<li class="page-item"><a class="page-link" href="#" data-page="${num}">${num}</a></li>`;
        }
    });

    html += `
                ${pagination.has_next ?
                    `<li class="page-item"><a class="page-link" href="#" data-page="${pagination.current_page + 1}">Next</a></li>` :
                    `<li class="page-item disabled"><span class="page-link">Next</span></li>`}
                </ul>
            </nav>
        </div>
    `;

    container.innerHTML = html;

    container.querySelectorAll(".page-link[data-page]").forEach(link => {
        link.addEventListener("click", function (e) {
            e.preventDefault();
            const page = parseInt(this.getAttribute("data-page"));
            if (tab === 'history') {
                if (!isNaN(page)) fetchHistoryData(page);
            }
            if (tab === 'vehicle_master') {
                if (!isNaN(page)) fetchVehicleMaster(page);
            }
        });
    });
}

function downloadVehicleMaster() {
    window.open('/api/download_vehicle_master/', '_blank');
}

function openEditModal(data) {
    document.getElementById('edit_rfid').value = data.rfid;
    document.getElementById('edit_vehicle_number').value = data.vehicle_number;
    document.getElementById('edit_vendor_name').value = data.vendor_name;
    document.getElementById('edit_vendor_code').value = data.vendor_code;

    new bootstrap.Modal(document.getElementById('editVehicleModal')).show();
}

function submitVehicleEdit() {
    const payload = {
        rfid: document.getElementById('edit_rfid').value,
        vehicle_number: document.getElementById('edit_vehicle_number').value,
        vendor_name: document.getElementById('edit_vendor_name').value,
        vendor_code: document.getElementById('edit_vendor_code').value,
    };

    fetch('/api/edit_vehicle_master/', {
        method: 'POST',
        headers: {
            "X-CSRFToken": csrftoken,
            "Content-Type": "application/json"
        },
        body: JSON.stringify(payload)
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            alert("Updated successfully");
            fetchVehicleMaster();
            bootstrap.Modal.getInstance(document.getElementById('editVehicleModal')).hide();
        } else {
            alert(res.error);
        }
    });
}

function uploadVehicleMaster() {
    const data = prompt("Paste JSON data");

    if (!data) return;

    fetch('/api/upload_vehicle_master/', {
        method: 'POST',
        headers: {
            "X-CSRFToken": csrftoken,
            "Content-Type": "application/json"
        },
        body: data
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            alert("Uploaded successfully");
            fetchVehicleMaster();
        } else {
            alert(res.error);
        }
    });
}

// POST REQUEST FOR ADDING VEHICLE AND VENDOR
document.getElementById('submitVehicleDetailsBtn').addEventListener("click", function () {

    const rfid = document.getElementById("rfidInput").value;
    const vehicleNumber = document.getElementById("vehicleNumberInput").value;

    const vendorNameInput = document.getElementById("vendorNameInput").value;
    const vendorCodeInput = document.getElementById("vendorCodeInput").value;

    let payload = {
        rfid: rfid,
        vehicleNumber: vehicleNumber,
        vendorName: vendorNameInput,
        vendorCode: vendorCodeInput,
        // bucketNo: bucketNo,
    };

    fetch(`/api/add_vehicle/`, {
        method: "POST",
        headers: { 
            "X-CSRFToken": csrftoken,
            "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            alert("Vehicle updated successfully");

            document.getElementById("vehicleDetailsForm").reset();

            const modalEl = document.getElementById("vehicleDetailsModal");
            bootstrap.Modal.getInstance(modalEl).hide();

            vehicleDetailsModalOpen = false;
        } else {
            alert(res.error || "Failed to update");
        }
    })
    .catch(err => console.error(err));
});

document.addEventListener('click', function (e) {
    const btn = e.target.closest('.view-image-btn');
    if (!btn) return;

    const vehicle_number = btn.dataset.vehicle_number;
    const imgSrc = btn.dataset.src;
    document.getElementById("vehicle_number").innerHTML = `Vehicle Number: <b>${vehicle_number}</b>`;

    const modalEl = document.getElementById('imageModal');
    modalEl.dataset.src = imgSrc;

    const modal = new bootstrap.Modal(modalEl);
    modal.show();
});

document.getElementById("imageModal")
    .addEventListener("hidden.bs.modal", function () {
        const img = document.getElementById("modalImage");
        const loader = document.getElementById("imageLoader");

        isImageLoading = false;
        img.src = "";
        img.style.display = "none";
        loader.style.display = "none";
});

document.getElementById("imageModal")
.addEventListener("shown.bs.modal", function () {

    const img = document.getElementById("modalImage");
    const loader = document.getElementById("imageLoader");
    const src = this.dataset.src;

    if (!src) return;

    isImageLoading = true;

    loader.style.display = "block";
    img.style.display = "none";

    img.onload = function () {
        loader.style.display = "none";
        img.style.display = "block";
        isImageLoading = false;
    };

    img.onerror = function () {
        loader.style.display = "none";
        img.style.display = "none";
        isImageLoading = false;
    };

    img.src = src;
});

function updateSystemStatus() {
    const cameraContainer = document.getElementById('camera-indicators');
    const locationContainer = document.getElementById('location-dots');
    
    cameraContainer.innerHTML = '';
    locationContainer.innerHTML = '';

    // Create invisible location info dots
    Object.entries(systemData).forEach(([key, info]) => {
        const dot = document.createElement('div');
        let statusClass = '';

        if (info.type && info.type == 'info') {
            dot.className = 'fs-5 location-dot';
        }
        else {
            if (info.status) statusClass = info.status.toLowerCase();
            dot.className = `cam-dot ${statusClass}`;
        }
        
        dot.style.top = `${info.top}%`;
        dot.style.left = `${info.left}%`;

        let tooltipText = `${key}\n`;

        if (info.name) tooltipText += `Name: ${info.name}\n`;

        if (info.current_stem) tooltipText += `Current Stem: ${info.current_stem}\n`;
        if (info.current_bunch) tooltipText += `Current Bunch: ${info.current_bunch}\n`;
        if (info.anodes) tooltipText += `Current Anodes: ${info.anodes}\n`;

        if (info.status) tooltipText += `Status: ${info.status}\n`;
        if (info.ip) tooltipText += `IP: ${info.ip}\n`;
        if (info.cam_serial) tooltipText += `Cam Serial: ${info.cam_serial}\n`;

        if (info.last_ping) tooltipText += `Last Ping: ${info.last_ping}\n`;

        dot.dataset.tooltip = tooltipText.trim();
        locationContainer.appendChild(dot);
    });
}

// Fetch from API (commented out for testing)
async function fetchSystemStatus() {
    try {
        // Uncomment when API is ready
        const response = await fetch('/api/health_status/');
        const data = await response.json();
        systemData = data.data;
        
        updateSystemStatus();
    } catch (error) {
        console.error('Error fetching system status:', error);
    }
}

function updateCameraImages() {
    const url = `/api/live-ip-camera`;
    fetch(url)
        .then(res => res.json())
        .then(data => {
            if (data.status === "success") {
                data.cameras.forEach((cam, idx) => {
                    const slot = document.getElementById(`camera-slot-${idx + 1}`);
                    if (!slot) return;
                    let img = slot.querySelector("img");

                    if (cam.image_url) {
                        if (!img) {
                            // Create img once if it doesn't exist
                            img = document.createElement("img");
                            slot.innerHTML = "";  // clear slot only first time
                            slot.appendChild(img);
                        }
                        // Update src without creating new element → no blink
                        img.src = `${cam.image_url}?t=${Date.now()}`;
                    } else {
                        slot.innerHTML = "No Camera Selected";
                    }
                });
            }
        })
        .catch(err => console.error("Error fetching camera images:", err));
}

// Update on user activity
function resetActivityTimer() {
    lastActivityTime = Date.now();
}

// Listen to common user actions
['click', 'mousemove', 'keydown', 'scroll', 'touchstart'].forEach(event => {
    document.addEventListener(event, resetActivityTimer, true);
});

// Initialize
document.addEventListener('DOMContentLoaded', function() {

    // First run
    fetchSystemStatus();
    updateCurrentStatus();
    updateCameraImages();

    // Polling loop
    setInterval(fetchSystemStatus, 5000);
    setInterval(updateCurrentStatus, 2000);
    setInterval(updateCameraImages, 2000);

    // Smart refresh
    setInterval(() => {
        const now = Date.now();
        const inactivityDuration = now - lastActivityTime;
    
        const REFRESH_TIMEOUT = 30 * 60 * 1000;
    
        if (inactivityDuration > REFRESH_TIMEOUT) {
            console.log("Auto-refreshing due to inactivity");
            window.location.reload();
        }
    }, 10 * 1000);

});