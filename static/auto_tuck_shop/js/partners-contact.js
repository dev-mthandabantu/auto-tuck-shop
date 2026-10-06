(function () {
  "use strict";

  var form = document.getElementById("partner-contact");
  if (!form) return;

  var email = form.dataset.email;
  var whatsappNumber = form.dataset.whatsapp;

  var panels = Array.prototype.slice.call(form.querySelectorAll(".contact-panel[data-step]"));
  var stepBars = Array.prototype.slice.call(form.querySelectorAll(".contact-step-bar"));
  var nav = form.querySelector(".contact-nav");
  var backBtn = nav.querySelector('[data-action="back"]');
  var nextBtn = nav.querySelector('[data-action="next"]');
  var preview = form.querySelector(".contact-preview");
  var previewText = form.querySelector(".contact-preview-text");
  var sendBlock = form.querySelector(".contact-send");
  var emailLink = form.querySelector('[data-action="send-email"]');
  var whatsappLink = form.querySelector('[data-action="send-whatsapp"]');
  var shopOwnerPanel = form.querySelector('[data-step="shop-owner"]');
  var restartBtn = form.querySelector('[data-action="restart"]');
  var stepsRow = form.querySelector(".contact-steps");

  var STEP_ORDER = [0, 1, 2];
  var current = 0;

  function otherInputFor(panel) {
    return panel ? panel.querySelector(".contact-other-input") : null;
  }

  function selectedValue(name) {
    var checked = form.querySelector('input[name="' + name + '"]:checked');
    if (!checked) return null;
    if (checked.value === "Other") {
      var other = form.querySelector('input[name="' + name + '_other"]');
      var text = other && other.value.trim();
      return text ? text : "Other (not specified)";
    }
    return checked.value;
  }

  function isWhoShopOwner() {
    var checked = form.querySelector('input[name="who"]:checked');
    return checked && checked.value === "Shop owner";
  }

  function stepIsAnswered(step) {
    if (step === 0) return customChoiceIsAnswered("who");
    if (step === 1) return customChoiceIsAnswered("topic");
    if (step === 2) {
      var name = form.querySelector('input[name="name"]').value.trim();
      var contact = form.querySelector('input[name="contact_info"]').value.trim();
      return name.length > 0 && contact.length > 0;
    }
    return false;
  }

  function customChoiceIsAnswered(name) {
    var checked = form.querySelector('input[name="' + name + '"]:checked');
    if (!checked) return false;
    if (checked.value !== "Other") return true;
    var other = form.querySelector('input[name="' + name + '_other"]');
    return !!(other && other.value.trim());
  }

  function showStep(step) {
    current = step;
    stepsRow.hidden = false;
    panels.forEach(function (p) {
      p.hidden = p.dataset.step !== String(step);
    });
    shopOwnerPanel.hidden = true;
    stepBars.forEach(function (bar) {
      var barStep = Number(bar.dataset.step);
      bar.classList.toggle("is-done", stepIsAnswered(barStep));
      bar.classList.toggle("is-active", !stepIsAnswered(barStep) && barStep === step);
    });
    backBtn.disabled = step === 0;
    var isFinal = step === STEP_ORDER[STEP_ORDER.length - 1];
    nextBtn.hidden = isFinal;
    nextBtn.disabled = !stepIsAnswered(step);
    nav.hidden = false;
    sendBlock.hidden = !isFinal;
    updatePreview();
    updateSendState();
  }

  function showShopOwnerRedirect() {
    stepsRow.hidden = true;
    panels.forEach(function (p) {
      p.hidden = true;
    });
    nav.hidden = true;
    sendBlock.hidden = true;
    preview.hidden = true;
    shopOwnerPanel.hidden = false;
  }

  function buildSummary() {
    var who = selectedValue("who") || "Not specified";
    var topic = selectedValue("topic") || "Not specified";
    var name = form.querySelector('input[name="name"]').value.trim() || "Not specified";
    var org = form.querySelector('input[name="organisation"]').value.trim() || "Not specified";
    var contactInfo = form.querySelector('input[name="contact_info"]').value.trim() || "Not specified";
    var message = form.querySelector('textarea[name="message"]').value.trim();

    var lines = [
      "Hello Auto Tuckshop team,",
      "",
      "I would like to get in touch about Auto Tuckshop.",
      "",
      "Who I am: " + who,
      "Topic: " + topic,
      "Name: " + name,
      "Organisation: " + org,
      "Email or phone: " + contactInfo,
    ];
    if (message) {
      lines.push("", "Message: " + message);
    }
    return lines.join("\n");
  }

  function updatePreview() {
    previewText.textContent = buildSummary();
  }

  function updateSendState() {
    var ready = stepIsAnswered(0) && stepIsAnswered(1) && stepIsAnswered(2);
    var summary = buildSummary();
    var who = selectedValue("who") || "";
    var org = form.querySelector('input[name="organisation"]').value.trim();
    var subject = "Auto Tuckshop: " + (selectedValue("topic") || "Enquiry") + (org ? " from " + org : "");

    emailLink.href = ready
      ? "mailto:" + email + "?subject=" + encodeURIComponent(subject) + "&body=" + encodeURIComponent(summary)
      : "#";
    emailLink.setAttribute("aria-disabled", String(!ready));
    emailLink.classList.toggle("is-disabled", !ready);

    if (whatsappNumber && whatsappLink) {
      whatsappLink.hidden = false;
      whatsappLink.href = ready
        ? "https://wa.me/" + whatsappNumber + "?text=" + encodeURIComponent(summary)
        : "#";
      whatsappLink.setAttribute("aria-disabled", String(!ready));
      whatsappLink.classList.toggle("is-disabled", !ready);
    } else if (whatsappLink) {
      whatsappLink.hidden = true;
    }
  }

  panels.forEach(function (panel) {
    var other = otherInputFor(panel);
    panel.addEventListener("change", function (event) {
      if (event.target.type === "radio") {
        if (other) {
          other.hidden = event.target.value !== "Other";
          if (other.hidden) other.value = "";
        }
        if (panel.dataset.step === "0" && isWhoShopOwner()) {
          showShopOwnerRedirect();
          return;
        }
      }
      updatePreview();
      updateSendState();
      nextBtn.disabled = !stepIsAnswered(current);
    });
    panel.addEventListener("input", function () {
      updatePreview();
      updateSendState();
      nextBtn.disabled = !stepIsAnswered(current);
    });
  });

  restartBtn.addEventListener("click", function () {
    form.querySelectorAll('input[name="who"]').forEach(function (input) {
      input.checked = false;
    });
    preview.hidden = false;
    showStep(0);
  });

  nextBtn.addEventListener("click", function () {
    if (!stepIsAnswered(current)) return;
    var nextIndex = STEP_ORDER.indexOf(current) + 1;
    if (nextIndex < STEP_ORDER.length) showStep(STEP_ORDER[nextIndex]);
  });

  backBtn.addEventListener("click", function () {
    var prevIndex = STEP_ORDER.indexOf(current) - 1;
    if (prevIndex >= 0) showStep(STEP_ORDER[prevIndex]);
  });

  [emailLink, whatsappLink].filter(Boolean).forEach(function (link) {
    link.addEventListener("click", function (event) {
      if (link.classList.contains("is-disabled")) event.preventDefault();
    });
  });

  form.hidden = false;
  showStep(0);
})();
