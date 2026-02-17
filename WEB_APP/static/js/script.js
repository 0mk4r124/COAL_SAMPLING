let currentPage = 1;
let scale = 1;
let isDragging = false;
let startX, startY, imgX = 0, imgY = 0;
let currentOpenedAnodeId = null;
let currentOpenedBunch = null;
let isImageLoading = false;
let carouselImages = [];
let currentImageIndex = 0;

const img = document.getElementById("modalImage");
const wrapper = document.getElementById("zoomWrapper");

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

document.addEventListener("DOMContentLoaded", function () {

    const img = document.getElementById("modalImage");
    const wrapper = document.getElementById("zoomWrapper");

    /* ---------------- IMAGE MODAL ZOOM ---------------- */

    if (wrapper && img) {

        // Reset zoom when image modal opens
        document.getElementById("imageModal")?.addEventListener("shown.bs.modal", () => {
            scale = 1;
            imgX = imgY = 0;
            img.style.transform = `translate(0px, 0px) scale(1)`;
        });

        // Mouse wheel zoom
        wrapper.addEventListener("wheel", (e) => {
            e.preventDefault();
            const delta = e.deltaY < 0 ? 0.1 : -0.1;
            scale = Math.min(Math.max(0.5, scale + delta), 4);
            img.style.transform = `translate(${imgX}px, ${imgY}px) scale(${scale})`;
        });

        // Drag start
        wrapper.addEventListener("mousedown", (e) => {
            isDragging = true;
            wrapper.style.cursor = "grabbing";
            startX = e.clientX - imgX;
            startY = e.clientY - imgY;
        });

        // Drag move
        wrapper.addEventListener("mousemove", (e) => {
            if (!isDragging) return;
            imgX = e.clientX - startX;
            imgY = e.clientY - startY;
            img.style.transform = `translate(${imgX}px, ${imgY}px) scale(${scale})`;
        });

        // Drag end
        wrapper.addEventListener("mouseup", () => {
            isDragging = false;
            wrapper.style.cursor = "grab";
        });

        wrapper.addEventListener("mouseleave", () => {
            isDragging = false;
        });
    }

    /* ---------------- IMAGE CAROUSEL MODAL ---------------- */

    document.getElementById("imageCarouselModal")?.addEventListener(
        "shown.bs.modal",
        function () {

            const modal = this;
            const loader = document.getElementById("carouselLoader");
            const counter = document.getElementById("carouselCounter");

            carouselImages = JSON.parse(modal.dataset.images || "[]");
            currentImageIndex = 0;

            if (!carouselImages.length || !img) return;

            const loadImage = (index) => {
                loader.style.display = "block";
                img.style.display = "none";

                scale = 1;
                imgX = imgY = 0;
                img.style.transform = `translate(0px, 0px) scale(1)`;

                img.onload = () => {
                    loader.style.display = "none";
                    img.style.display = "block";
                };

                img.onerror = () => {
                    loader.style.display = "none";
                };

                requestAnimationFrame(() => {
                    img.src = carouselImages[index];
                });

                counter.textContent = `${index + 1} / ${carouselImages.length}`;
            };

            loadImage(currentImageIndex);

            document.getElementById("prevImageBtn").onclick = () => {
                if (currentImageIndex > 0) {
                    currentImageIndex--;
                    loadImage(currentImageIndex);
                }
            };

            document.getElementById("nextImageBtn").onclick = () => {
                if (currentImageIndex < carouselImages.length - 1) {
                    currentImageIndex++;
                    loadImage(currentImageIndex);
                }
            };
        }
    );

    /* ---------------- VEHICLE MODAL ---------------- */

    document.getElementById("vehicleDetailsModal")?.addEventListener(
        "shown.bs.modal",
        function () {

            const modal = this;
            const rfidInput = document.getElementById("rfidInput");
            const vehicleInput = document.getElementById("vehicleNumberInput");
            const vendorInput = document.getElementById("vendorNameInput");

            if (!rfidInput || !vehicleInput || !vendorInput) return;

            vehicleInput.value = "";
            vendorInput.value = "";
            rfidInput.value = modal.dataset.rfid || "";
        }
    );

    /* ---------------- DATE RANGE PICKER ---------------- */

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
    });

    $('#date_range').on("apply.daterangepicker", function (ev, picker) {
        const start = picker.startDate.format("YYYY-MM-DD");
        const end = picker.endDate.format("YYYY-MM-DD");
        $(this).val(start === end ? start : `${start} - ${end}`);
    });

    $('#date_range').on("cancel.daterangepicker", function () {
        $(this).val("");
    });

});

function openAddVehicleModal(rfidFromBE) {
    const modal = document.getElementById("vehicleDetailsModal");
    modal.dataset.rfid = rfidFromBE;
    new bootstrap.Modal(modal).show();
}

function openImageCarouselModal(imagesArray) {
    const modal = document.getElementById("imageCarouselModal");
    modal.dataset.images = JSON.stringify(imagesArray);
    new bootstrap.Modal(modal).show();
}
